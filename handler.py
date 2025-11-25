import os
import time
import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import requests
import runpod

# --------------------
# Basic config / paths
# --------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

# In the base image COMFY_DIR is /comfyui. We still allow overriding via env.
COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")

# For inputs/outputs we default to the network volume layout, but allow env overrides.
INPUT_DIR = os.getenv("INPUT_DIR", "/runpod-volume/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/runpod-volume/ComfyUI/output")

# Comfy log path (for dump_comfy_log debugging helper)
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/comfyui/user/comfyui.log")

# Where we expect to find PNGs for list_all_outputs scanning
OUTPUT_SCAN_DIRS: List[str] = [
    OUTPUT_DIR,
    "/comfyui/output",
    "/comfyui/user/output",
    "/comfyui/user",
    "/workspace/ComfyUI/output",
]

# Seconds to wait for ComfyUI to come up / complete a prompt
COMFY_START_TIMEOUT = int(os.getenv("COMFY_START_TIMEOUT", "120"))
COMFY_REQUEST_TIMEOUT = int(os.getenv("COMFY_REQUEST_TIMEOUT", "120"))

# --------------------
# Logging
# --------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("handler")

# --------------------
# ComfyUI process management
# --------------------

COMFY_PROCESS: Optional[subprocess.Popen] = None


def _start_comfy_if_needed() -> None:
    """
    Ensure the embedded ComfyUI process is running.

    The base image's entrypoint already uses this pattern; we replicate it here
    so serverless cold starts are robust.
    """
    global COMFY_PROCESS

    if COMFY_PROCESS is not None and COMFY_PROCESS.poll() is None:
        return

    logger.info("Starting ComfyUI process in %s", COMFY_DIR)
    cmd = [
        "python",
        os.path.join(COMFY_DIR, "main.py"),
        "--listen",
        "0.0.0.0",
        "--port",
        str(COMFY_PORT),
    ]
    # Run in its own process group so RunPod can SIGTERM cleanly.
    COMFY_PROCESS = subprocess.Popen(
        cmd,
        cwd=COMFY_DIR,
    )


def _wait_for_comfy_ready(timeout: int = COMFY_START_TIMEOUT) -> None:
    """
    Poll /system_stats until ComfyUI responds or timeout.
    """
    deadline = time.time() + timeout
    last_error: Optional[str] = None

    while time.time() < deadline:
        try:
            resp = requests.get(f"{COMFY_BASE}/system_stats", timeout=5)
            if resp.ok:
                return
            last_error = f"http {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
        time.sleep(1)

    raise RuntimeError(f"ComfyUI not ready after {timeout}s (last_error={last_error})")


def _ensure_comfy_ready() -> None:
    _start_comfy_if_needed()
    _wait_for_comfy_ready()


# --------------------
# Small helpers
# --------------------

def _ok(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _fail(message: str, **extra: Any) -> Dict[str, Any]:
    logger.error("handler failure: %s", message)
    payload: Dict[str, Any] = {"message": message}
    payload.update(extra)
    return {"ok": False, "data": payload}


def _comfy_get(path: str, **kwargs: Any) -> requests.Response:
    url = f"{COMFY_BASE}{path}"
    logger.info("GET %s", url)
    resp = requests.get(url, timeout=COMFY_REQUEST_TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp


def _comfy_post(path: str, json: Dict[str, Any], **kwargs: Any) -> requests.Response:
    url = f"{COMFY_BASE}{path}"
    logger.info("POST %s", url)
    resp = requests.post(url, json=json, timeout=COMFY_REQUEST_TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp


def _scan_outputs(dirs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Scan one or more output directories for PNGs.

    Returns a list of {name, path, size, mtime} sorted by mtime ascending.
    """
    scan_dirs = dirs or [OUTPUT_DIR]
    seen: Dict[str, Dict[str, Any]] = {}

    for d in scan_dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            for name in os.listdir(d):
                if not name.lower().endswith(".png"):
                    continue
                path = os.path.join(d, name)
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue
                key = f"{d}:{name}"
                # Keep the newest mtime for duplicates in different dirs
                info = {
                    "name": name,
                    "path": path,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
                if key not in seen or seen[key]["mtime"] < info["mtime"]:
                    seen[key] = info
        except FileNotFoundError:
            continue

    images = list(seen.values())
    images.sort(key=lambda x: x["mtime"])
    return images


def _await_new_outputs(
    before: List[Dict[str, Any]],
    wait_seconds: int = 40,
    poll_interval: int = 4,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Poll OUTPUT_SCAN_DIRS until new PNGs appear compared to `before`.

    Returns (new_images, after_all_images).
    """
    before_keys = {(img["path"], img["mtime"]) for img in before}
    deadline = time.time() + wait_seconds

    while time.time() < deadline:
        after = _scan_outputs(OUTPUT_SCAN_DIRS)
        # New images = anything with a (path,mtime) not seen in `before`
        new = [img for img in after if (img["path"], img["mtime"]) not in before_keys]
        if new:
            new.sort(key=lambda x: x["mtime"], reverse=True)
            return new, after

        time.sleep(poll_interval)

    # Timed out – return empty list plus latest snapshot
    after = _scan_outputs(OUTPUT_SCAN_DIRS)
    return [], after


def _tail_file(path: str, lines: int = 200) -> str:
    """
    Simple tail implementation – fine for the Comfy log sizes we're dealing with.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().splitlines()
    except FileNotFoundError:
        return f"(log file not found: {path})"

    if not content:
        return ""
    return "\n".join(content[-lines:])


# --------------------
# Action handlers
# --------------------

def _handle_ping(_: Dict[str, Any]) -> Dict[str, Any]:
    return _ok({"message": "pong"})


def _handle_about(_: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(
        {
            "service": "runpod-comfy-flux-ip",
            "version": "v13",
            "env": {
                "COMFY_DIR": COMFY_DIR,
                "INPUT_DIR": INPUT_DIR,
                "OUTPUT_DIR": OUTPUT_DIR,
            },
        }
    )


def _handle_preflight(_: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_comfy_ready()
    try:
        resp = _comfy_get("/system_stats")
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return _fail(f"failed to fetch /system_stats: {e}")
    return _ok({"system_stats": data})


def _handle_upload_input_url(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download an image from a URL and save it to INPUT_DIR.

    Request shape:
      { "url": "...", "filename": "ip_ref.png" }
    """
    url = body.get("url")
    filename = body.get("filename") or "input.png"
    if not url:
        return _fail("upload_input_url requires 'url'")

    os.makedirs(INPUT_DIR, exist_ok=True)
    dest_path = os.path.join(INPUT_DIR, filename)

    logger.info("Downloading %s -> %s", url, dest_path)
    try:
        with requests.get(url, stream=True, timeout=COMFY_REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
    except Exception as e:  # noqa: BLE001
        return _fail(f"failed to download url: {e}")

    return _ok({"saved": dest_path})


def _handle_dump_comfy_log(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the tail of the Comfy log.

    If COMFY_LOG_PATH doesn't exist, we fall back to scanning COMFY_DIR for
    any comfyui.log and use the first one we find.
    """
    lines = int(body.get("lines") or 200)
    path = COMFY_LOG_PATH
    tail = _tail_file(path, lines=lines)

    if tail.startswith("(log file not found:"):
        # Fallback search – base image versions sometimes move the log
        alt_path: Optional[str] = None
        for root, _dirs, files in os.walk(COMFY_DIR):
            if "comfyui.log" in files:
                alt_path = os.path.join(root, "comfyui.log")
                break
        if alt_path:
            path = alt_path
            tail = _tail_file(path, lines=lines)

    return _ok({"log_tail": tail, "path": path})


def _handle_comfy_get(body: Dict[str, Any]) -> Dict[str, Any]:
    path = body.get("path")
    if not path:
        return _fail("comfy_get requires 'path'")
    if not path.startswith("/"):
        path = "/" + path

    try:
        resp = _comfy_get(path)
        # Try JSON, but fall back to raw text for debugging
        try:
            data = resp.json()
        except ValueError:
            data = None
        return _ok({"path": path, "data": data, "raw": resp.text})
    except Exception as e:  # noqa: BLE001
        return _fail(f"comfy_get error for {path}: {e}")


def _handle_history(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch Comfy history for a given prompt_id.

    We try GET /history/{prompt_id} (newer Comfy) first. If that fails,
    we fall back to POST /history.

    Whatever happens, we return:
      - history: parsed JSON if available, else None
      - raw: raw response body (to see errors when JSON parsing fails)
    """
    payload = body.get("payload") or {}
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        return _fail("history requires payload.prompt_id")

    last_error: Optional[str] = None

    # Try GET /history/{prompt_id}
    try:
        resp = _comfy_get(f"/history/{prompt_id}")
        try:
            data = resp.json()
        except ValueError as e:  # not JSON
            return _ok(
                {
                    "prompt_id": prompt_id,
                    "history": None,
                    "raw": resp.text,
                    "parse_error": f"GET /history/{prompt_id} JSON error: {e}",
                }
            )
        else:
            return _ok({"prompt_id": prompt_id, "history": data, "raw": resp.text})
    except Exception as e:  # noqa: BLE001
        last_error = f"GET /history/{prompt_id} failed: {e}"

    # Fallback: POST /history
    try:
        resp = _comfy_post("/history", json={"prompt_id": prompt_id})
        try:
            data = resp.json()
        except ValueError as e:  # not JSON
            return _ok(
                {
                    "prompt_id": prompt_id,
                    "history": None,
                    "raw": resp.text,
                    "parse_error": f"POST /history JSON error: {e}",
                }
            )
        else:
            return _ok({"prompt_id": prompt_id, "history": data, "raw": resp.text})
    except Exception as e:  # noqa: BLE001
        last_error = f"{last_error}; POST /history failed: {e}" if last_error else str(e)
        return _fail(f"history error for {prompt_id}: {last_error}")


def _handle_list_outputs(_: Dict[str, Any]) -> Dict[str, Any]:
    images = _scan_outputs([OUTPUT_DIR])
    return _ok({"images": images})


def _handle_list_all_outputs(_: Dict[str, Any]) -> Dict[str, Any]:
    images = _scan_outputs(OUTPUT_SCAN_DIRS)
    return _ok({"images": images, "scan_dirs": OUTPUT_SCAN_DIRS})


def _handle_debug_ip_paths(_: Dict[str, Any]) -> Dict[str, Any]:
    """
    Helper used while wiring up the Flux IP Adapter.

    We try to inspect the LoadFluxIPAdapter node via /object_info so you can see
    what file names Comfy is expecting, and we also do a best-effort scan of
    /runpod-volume/models for matching safetensors.
    """
    info: Dict[str, Any] = {}
    try:
        resp = _comfy_get("/object_info")
        obj = resp.json()
        nodes = obj.get("nodes") or obj
        lfip = nodes.get("LoadFluxIPAdapter", {})
        required = (lfip.get("input") or {}).get("required", {})
        clip_field: List[List[str]] = []
        ipad_field: List[List[str]] = []
        for field_name, meta in required.items():
            default = meta.get("default")
            if not isinstance(default, str):
                continue
            base = os.path.basename(default)
            if "clip" in field_name or "vision" in field_name:
                clip_field.append([field_name, base])
            if "ip" in field_name or "adapter" in field_name:
                ipad_field.append([field_name, base])
        info["LoadFluxIPAdapter"] = {
            "clip_vision_field": clip_field,
            "ipadatper_field": ipad_field,
        }
    except Exception as e:  # noqa: BLE001
        info["LoadFluxIPAdapter_error"] = str(e)

    # Also scan the models tree so we can see what's actually present.
    model_root = "/runpod-volume/models"
    found: List[str] = []
    if os.path.isdir(model_root):
        for root, _dirs, files in os.walk(model_root):
            for name in files:
                if name.lower().endswith(".safetensors") and (
                    "ip_adapter" in name.lower() or "clip" in name.lower()
                ):
                    found.append(os.path.join(root, name))

    info["matching_safetensors"] = sorted(found)
    info["model_root"] = model_root

    return _ok(info)


def _handle_generate(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic generate entrypoint.

    Expects:
      {
        "payload": {
          "workflow": {
            "client_id": "...",
            "prompt": { ... full Comfy prompt graph ... }
          }
        }
      }
    """
    _ensure_comfy_ready()

    payload = body.get("payload") or {}
    workflow = payload.get("workflow") or payload.get("comfy_prompt")
    if not workflow:
        return _fail("generate requires payload.workflow (Comfy prompt JSON)")

    if not isinstance(workflow, dict):
        return _fail("payload.workflow must be an object")

    before = _scan_outputs(OUTPUT_SCAN_DIRS)

    try:
        resp = _comfy_post("/prompt", json=workflow)
        prompt_resp = resp.json()
        prompt_id = prompt_resp.get("prompt_id")
    except Exception as e:  # noqa: BLE001
        return _fail(f"failed to POST /prompt: {e}")

    # Wait for new PNGs to appear
    wait_seconds = int(payload.get("wait_seconds") or 40)
    poll_interval = int(payload.get("poll_interval") or 4)
    new_images, after = _await_new_outputs(
        before,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )

    latest_image = new_images[0] if new_images else None

    return _ok(
        {
            "prompt_response": prompt_resp,
            "prompt_id": prompt_id,
            "new_images": new_images,
            "latest_image": latest_image,
            "wait_info": {
                "before_count": len(before),
                "after_count": len(after),
                "output_scan_dirs": OUTPUT_SCAN_DIRS,
                "wait_seconds": wait_seconds,
                "poll_interval": poll_interval,
            },
        }
    )


# --------------------
# RunPod handler entrypoint
# --------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod serverless handler.

    Expects `event` to contain an `input` dict with at least an `action` key.
    """
    inp = event.get("input") or {}
    action = inp.get("action") or "ping"

    logger.info("handler action=%s", action)

    try:
        # Simple actions
        if action == "ping":
            return _handle_ping(inp)
        if action == "about":
            return _handle_about(inp)
        if action == "preflight":
            return _handle_preflight(inp)
        if action == "upload_input_url":
            return _handle_upload_input_url(inp)
        if action == "dump_comfy_log":
            return _handle_dump_comfy_log(inp)
        if action == "comfy_get":
            return _handle_comfy_get(inp)
        if action == "history":
            return _handle_history(inp)
        if action == "list_outputs":
            return _handle_list_outputs(inp)
        if action == "list_all_outputs":
            return _handle_list_all_outputs(inp)
        if action == "debug_ip_paths":
            return _handle_debug_ip_paths(inp)

        # Generation aliases – all funnel into the same implementation
        if action in {
            "generate",
            "generate_flux",
            "generate_flux_base",
            "generate_flux_ip",
        }:
            return _handle_generate(inp)

        return _fail(f"Unknown or missing action: {action!r}")

    except Exception as e:  # noqa: BLE001
        # Catch-all so we *always* return a JSON payload to RunPod.
        logger.exception("Unhandled error in handler for action=%s", action)
        return _fail(f"unhandled error: {e.__class__.__name__}: {e}")


# --------------------
# RunPod serverless bootstrap
# --------------------

runpod.serverless.start({"handler": handler})
