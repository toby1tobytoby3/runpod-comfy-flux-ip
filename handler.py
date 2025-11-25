import json
import logging
import os
import pathlib
import time
from typing import Any, Dict, List, Optional, Tuple

import runpod
import requests
import subprocess

# -------------------------------------------------------------------
# Basic config
# -------------------------------------------------------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

# ComfyUI install dir inside the container
COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")

# Python executable to run ComfyUI
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "/opt/venv/bin/python")

# Where we tee ComfyUI stdout/stderr
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")

# HTTP timeout for ComfyUI requests
COMFY_REQUEST_TIMEOUT = float(os.getenv("COMFY_REQUEST_TIMEOUT", "60.0"))

# Network volume input/output (mounted by RunPod)
INPUT_DIR = os.getenv("INPUT_DIR", "/runpod-volume/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/runpod-volume/ComfyUI/output")

# Directories we scan for new images
OUTPUT_SCAN_DIRS: List[str] = list(
    dict.fromkeys(
        [
            OUTPUT_DIR,
            "/runpod-volume/ComfyUI/output",
            "/comfyui/output",
            "/comfyui/user/output",
            "/workspace/ComfyUI/output",
        ]
    )
)

# Ensure directories exist
for d in [INPUT_DIR, OUTPUT_DIR]:
    pathlib.Path(d).mkdir(parents=True, exist_ok=True)

for d in OUTPUT_SCAN_DIRS:
    pathlib.Path(d).mkdir(parents=True, exist_ok=True)

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("handler")

# -------------------------------------------------------------------
# ComfyUI process management
# -------------------------------------------------------------------

_comfy_process: Optional[subprocess.Popen] = None


def _start_comfy_if_needed() -> None:
    """
    Start ComfyUI in the background if it isn't already running.
    Stdout/stderr are written to COMFY_LOG_PATH so we can tail them later.
    """
    global _comfy_process

    if _comfy_process is not None and _comfy_process.poll() is None:
        return

    cmd = [
        COMFY_PYTHON,
        "main.py",
        "--listen",
        "0.0.0.0",
        "--port",
        str(COMFY_PORT),
    ]

    log_path = pathlib.Path(COMFY_LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", buffering=1)

    _comfy_process = subprocess.Popen(
        cmd,
        cwd=COMFY_DIR,
        stdout=log_file,
        stderr=log_file,
        text=True,
    )

    logger.info("Started ComfyUI: pid=%s cmd=%s log=%s", _comfy_process.pid, cmd, COMFY_LOG_PATH)


def _wait_for_comfy_ready(timeout: float = 60.0) -> None:
    """
    Poll /system_stats until ComfyUI is ready or timeout.
    """
    start = time.time()
    last_err: Optional[str] = None

    while True:
        try:
            resp = requests.get(f"{COMFY_BASE}/system_stats", timeout=5)
            if resp.ok:
                logger.info("ComfyUI /system_stats OK")
                return
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)

        if time.time() - start > timeout:
            raise RuntimeError(f"Timed out waiting for ComfyUI: last_err={last_err}")

        time.sleep(1.0)


def _ensure_comfy_ready() -> None:
    """
    Ensure ComfyUI process is running and answering /system_stats.
    """
    _start_comfy_if_needed()
    _wait_for_comfy_ready()


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _fail(message: str, *, error: Optional[str] = None) -> Dict[str, Any]:
    logger.error("handler error: %s", message)
    return {"ok": False, "error": error or message}


def _comfy_get(
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> requests.Response:
    url = f"{COMFY_BASE}{path}"
    resp = requests.get(url, params=params, timeout=timeout or COMFY_REQUEST_TIMEOUT)
    return resp


def _comfy_post(
    path: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> requests.Response:
    url = f"{COMFY_BASE}{path}"
    resp = requests.post(url, json=json, timeout=timeout or COMFY_REQUEST_TIMEOUT)
    return resp


def _scan_outputs() -> List[Dict[str, Any]]:
    """
    Scan OUTPUT_SCAN_DIRS for PNG files and return metadata sorted by mtime.
    """
    images: List[Dict[str, Any]] = []

    for base_dir in OUTPUT_SCAN_DIRS:
        base_path = pathlib.Path(base_dir)
        if not base_path.exists():
            continue

        for root, _dirs, files in os.walk(base_path):
            for fname in files:
                if not fname.lower().endswith(".png"):
                    continue
                p = pathlib.Path(root) / fname
                try:
                    stat = p.stat()
                except OSError:
                    continue

                images.append(
                    {
                        "path": str(p),
                        "name": p.name,
                        "mtime": int(stat.st_mtime),
                        "size": stat.st_size,
                    }
                )

    images.sort(key=lambda x: x["mtime"])
    return images


def _await_new_outputs(
    before: Dict[str, int],
    wait_seconds: float = 40.0,
    poll_interval: float = 4.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Poll output dirs for up to wait_seconds, looking for PNGs whose
    mtime is newer than entries in `before`.
    """
    deadline = time.time() + wait_seconds

    while True:
        all_images = _scan_outputs()
        current_map = {img["path"]: img["mtime"] for img in all_images}

        new_images = [
            img
            for img in all_images
            if img["path"] not in before or img["mtime"] > before[img["path"]]
        ]

        if new_images:
            return new_images, current_map

        if time.time() >= deadline:
            return [], current_map

        time.sleep(poll_interval)


def _tail_file(path: str, max_bytes: int = 8192) -> str:
    """
    Tail up to max_bytes from the end of the file at `path`.
    Returns a friendly error string if missing/unreadable.
    """
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return f"(log file not found: {path})"

        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            chunk = f.read().decode("utf-8", errors="replace")
        return chunk
    except Exception as e:  # noqa: BLE001
        return f"(error reading log file {path}: {e})"


# -------------------------------------------------------------------
# Action handlers
# -------------------------------------------------------------------

def _handle_ping() -> Dict[str, Any]:
    return _ok({"message": "pong"})


def _handle_about() -> Dict[str, Any]:
    """
    Lightweight metadata about this worker.
    """
    return _ok(
        {
            "service": "runpod-comfy-flux-ip",
            "version": "v13",
            "comfy": {
                "base_url": COMFY_BASE,
                "dir": COMFY_DIR,
            },
            "paths": {
                "input_dir": INPUT_DIR,
                "output_dir": OUTPUT_DIR,
                "output_scan_dirs": OUTPUT_SCAN_DIRS,
                "comfy_log_path": COMFY_LOG_PATH,
            },
            "features": {
                "ping": True,
                "preflight": True,
                "generate": True,
                "ip_adapter": True,
                "history": True,
                "list_outputs": True,
                "list_all_outputs": True,
                "dump_comfy_log": True,
            },
        }
    )


def _handle_preflight() -> Dict[str, Any]:
    """
    Ensure ComfyUI is up and return /system_stats.
    """
    try:
        _ensure_comfy_ready()
        resp = _comfy_get("/system_stats")
        resp.raise_for_status()
        stats = resp.json()
    except Exception as e:  # noqa: BLE001
        return _fail(f"preflight failed: {e}")

    return _ok({"system_stats": stats})


def _handle_upload_input_url(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download an image from a public URL and save into INPUT_DIR.
    """
    payload = body.get("payload") or {}
    url = payload.get("url")
    filename = payload.get("filename") or "input.png"

    if not url:
        return _fail("upload_input_url requires payload.url")

    dest = pathlib.Path(INPUT_DIR) / filename
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        logger.info("Downloading input url=%s -> %s", url, dest)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    except Exception as e:  # noqa: BLE001
        return _fail(f"upload_input_url failed: {e}")

    return _ok({"saved": str(dest)})


def _handle_dump_comfy_log(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the tail of the ComfyUI log.

    - First, tail COMFY_LOG_PATH (what we configure for stdout/stderr).
    - If that file doesn't exist, we try to find a comfyui.log under COMFY_DIR.
    - Always return a friendly string; never throw.
    """
    payload = body.get("payload") or {}
    lines = int(payload.get("lines") or body.get("lines") or 400)

    primary_path = COMFY_LOG_PATH
    tail = _tail_file(primary_path)

    # If primary path missing, try to auto-discover comfyui.log under COMFY_DIR
    if tail.startswith("(log file not found"):
        alt_path: Optional[str] = None
        try:
            for root, _dirs, files in os.walk(COMFY_DIR):
                if "comfyui.log" in files:
                    alt_path = os.path.join(root, "comfyui.log")
                    break
        except Exception:  # noqa: BLE001
            alt_path = None

        if alt_path:
            tail = _tail_file(alt_path)
            target_path = alt_path
        else:
            target_path = primary_path
    else:
        target_path = primary_path

    # Trim to last N lines if needed
    tail_lines = tail.splitlines()
    if len(tail_lines) > lines:
        tail = "\n".join(tail_lines[-lines:])

    return _ok({"log_tail": tail, "path": target_path})


def _handle_comfy_get(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Passthrough GET to arbitrary ComfyUI path.
    Use with care; primarily for debugging.
    """
    payload = body.get("payload") or {}
    path = payload.get("path")
    params = payload.get("params") or {}

    if not path:
        return _fail("comfy_get requires payload.path")

    try:
        _ensure_comfy_ready()
        resp = _comfy_get(path, params=params)
        content_type = resp.headers.get("content-type", "")
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001
            parsed = resp.text
    except Exception as e:  # noqa: BLE001
        return _fail(f"comfy_get failed: {e}")

    return _ok(
        {
            "path": path,
            "status_code": resp.status_code,
            "content_type": content_type,
            "body": parsed,
        }
    )


def _handle_history(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch ComfyUI history for a given prompt_id.

    1) Try GET /history/{prompt_id} (newer API).
    2) If that fails or returns non-JSON, fall back to POST /history.
    3) If JSON parsing fails, return the raw text to help debugging instead
       of failing the whole request.
    """
    payload = body.get("payload") or {}
    prompt_id = payload.get("prompt_id")

    if not prompt_id:
        return _fail("history requires payload.prompt_id")

    try:
        _ensure_comfy_ready()

        # First try GET /history/{id}
        try:
            resp = _comfy_get(f"/history/{prompt_id}")
            if resp.ok:
                try:
                    data = resp.json()
                    return _ok({"prompt_id": prompt_id, "history": data})
                except Exception as e:  # noqa: BLE001
                    # Non-JSON body; return raw
                    return _ok(
                        {
                            "prompt_id": prompt_id,
                            "history": None,
                            "raw": resp.text,
                            "parse_error": f"GET /history/{prompt_id} JSON parse failed: {e}",
                        }
                    )
        except Exception:
            # We'll fall back to POST below
            pass

        # Legacy POST /history
        resp = _comfy_post("/history", json={"prompt_id": prompt_id})
        if not resp.ok:
            return _fail(
                f"history POST failed: HTTP {resp.status_code} for {prompt_id}"
            )

        try:
            data = resp.json()
            return _ok({"prompt_id": prompt_id, "history": data})
        except Exception as e:  # noqa: BLE001
            return _ok(
                {
                    "prompt_id": prompt_id,
                    "history": None,
                    "raw": resp.text,
                    "parse_error": f"POST /history JSON parse failed: {e}",
                }
            )

    except Exception as e:  # noqa: BLE001
        return _fail(f"history error for {prompt_id}: {e}")


def _handle_list_outputs(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    List PNG files in a single directory (default: OUTPUT_DIR).
    """
    payload = body.get("payload") or {}
    path = payload.get("path") or OUTPUT_DIR

    root = pathlib.Path(path)
    if not root.exists():
        return _ok({"images": [], "path": path})

    images: List[Dict[str, Any]] = []
    for p in root.glob("*.png"):
        try:
            stat = p.stat()
        except OSError:
            continue
        images.append(
            {
                "path": str(p),
                "name": p.name,
                "mtime": int(stat.st_mtime),
                "size": stat.st_size,
            }
        )

    images.sort(key=lambda x: x["mtime"])
    return _ok({"images": images, "path": path})


def _handle_list_all_outputs() -> Dict[str, Any]:
    """
    List PNG outputs across OUTPUT_SCAN_DIRS.
    """
    images = _scan_outputs()
    return _ok({"images": images, "scan_dirs": OUTPUT_SCAN_DIRS})


def _handle_debug_ip_paths() -> Dict[str, Any]:
    """
    Static debug helper for Flux IP-Adapter paths/fields.
    This does NOT touch the filesystem or models, just returns
    the expected field names you should use in workflows.
    """
    return _ok(
        {
            "LoadFluxIPAdapter": {
                "clip_vision_field": [["model.safetensors", "sigclip_vision_patch14_384.safetensors"]],
                "ipadatper_field": [["ip_adapter.safetensors"]],
            }
        }
    )


def _handle_generate(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a ComfyUI workflow (Flux base or Flux+IP) and return
    information about any new PNGs detected in the output dirs.

    Input contract (body.input.payload):
      - workflow: dict representing the full ComfyUI prompt
      - client_id: optional string (default: "imagineworlds")
      - wait_seconds: optional float (default: 40)
      - poll_interval: optional float (default: 4)
    """
    payload = body.get("payload") or {}
    workflow = payload.get("workflow")
    client_id = payload.get("client_id", "imagineworlds")

    if not workflow:
        return _fail("generate requires payload.workflow")

    wait_seconds = float(payload.get("wait_seconds") or 40.0)
    poll_interval = float(payload.get("poll_interval") or 4.0)

    try:
        _ensure_comfy_ready()
    except Exception as e:  # noqa: BLE001
        return _fail(f"generate preflight failed: {e}")

    # Record current outputs so we can diff after generation
    before_images = _scan_outputs()
    before_map = {img["path"]: img["mtime"] for img in before_images}

    try:
        if isinstance(workflow, dict) and "client_id" in workflow:
            workflow.pop("client_id")

        # Handle both workflow and prompt payloads gracefully
        if "workflow" in payload:
            req = {"client_id": client_id, "prompt": payload["workflow"]}
        elif "prompt" in payload:
            req = {"client_id": client_id, "prompt": payload["prompt"]}
        else:
            raise ValueError("Missing workflow or prompt in input payload")        logger.info("Submitting prompt to ComfyUI (client_id=%s)", client_id)
        resp = _comfy_post("/prompt", json=req, timeout=COMFY_REQUEST_TIMEOUT)
        resp.raise_for_status()
        prompt_response = resp.json()
        prompt_id = prompt_response.get("prompt_id")
    except Exception as e:  # noqa: BLE001
        return _fail(f"generate failed: {e}")

    # Wait for new PNGs to land in output dirs
    new_images, after_map = _await_new_outputs(
        before_map,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )

    latest_image: Optional[Dict[str, Any]] = None
    if new_images:
        latest_image = sorted(new_images, key=lambda x: x["mtime"])[-1]

    wait_info = {
        "before_count": len(before_map),
        "after_count": len(after_map),
        "output_scan_dirs": OUTPUT_SCAN_DIRS,
        "wait_seconds": wait_seconds,
        "poll_interval": poll_interval,
    }

    return _ok(
        {
            "prompt_id": prompt_id,
            "prompt_response": prompt_response,
            "wait_info": wait_info,
            "new_images": new_images,
            "latest_image": latest_image,
        }
    )


# -------------------------------------------------------------------
# RunPod entrypoint
# -------------------------------------------------------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod serverless handler.

    Expected event shape:
      {
        "input": {
          "action": "...",
          "payload": { ... }   # optional
        }
      }
    """
    inp = (event or {}).get("input") or {}
    action = inp.get("action")
    payload = inp.get("payload")  # may be None

    logger.info("handler action=%s", action)

    # Normalise body we pass to handlers
    body = {"payload": payload} if isinstance(payload, dict) else {}

    try:
        if action == "ping":
            return _handle_ping()
        if action == "about":
            return _handle_about()
        if action == "preflight":
            return _handle_preflight()
        if action == "upload_input_url":
            return _handle_upload_input_url(body)
        if action == "dump_comfy_log":
            return _handle_dump_comfy_log(body)
        if action == "comfy_get":
            return _handle_comfy_get(body)
        if action == "history":
            return _handle_history(body)
        if action == "list_outputs":
            return _handle_list_outputs(body)
        if action == "list_all_outputs":
            return _handle_list_all_outputs()
        if action == "debug_ip_paths":
            return _handle_debug_ip_paths()
        if action == "generate":
            # Generic generate (Flux base, Flux+IP, etc.)
            return _handle_generate(body)

        return _fail(f"unknown action: {action}")
    except Exception as e:  # noqa: BLE001
        logger.exception("Unhandled exception in handler")
        return _fail(f"unhandled exception: {e}")


runpod.serverless.start({"handler": handler})
