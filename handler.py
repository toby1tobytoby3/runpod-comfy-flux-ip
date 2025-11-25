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
COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "/opt/venv/bin/python")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")
COMFY_REQUEST_TIMEOUT = float(os.getenv("COMFY_REQUEST_TIMEOUT", "60.0"))

INPUT_DIR = os.getenv("INPUT_DIR", "/runpod-volume/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/runpod-volume/ComfyUI/output")

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
    start = time.time()
    last_err: Optional[str] = None
    while True:
        try:
            resp = requests.get(f"{COMFY_BASE}/system_stats", timeout=5)
            if resp.ok:
                logger.info("ComfyUI /system_stats OK")
                return
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = repr(e)
        if time.time() - start > timeout:
            raise RuntimeError(f"Timed out waiting for ComfyUI: last_err={last_err}")
        time.sleep(1.0)


def _ensure_comfy_ready() -> None:
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


def _comfy_get(path: str, *, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> requests.Response:
    return requests.get(f"{COMFY_BASE}{path}", params=params, timeout=timeout or COMFY_REQUEST_TIMEOUT)


def _comfy_post(path: str, *, json: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> requests.Response:
    return requests.post(f"{COMFY_BASE}{path}", json=json, timeout=timeout or COMFY_REQUEST_TIMEOUT)


def _scan_outputs() -> List[Dict[str, Any]]:
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
                    {"path": str(p), "name": p.name, "mtime": int(stat.st_mtime), "size": stat.st_size}
                )
    images.sort(key=lambda x: x["mtime"])
    return images


def _await_new_outputs(before: Dict[str, int], wait_seconds: float = 40.0, poll_interval: float = 4.0):
    deadline = time.time() + wait_seconds
    while True:
        all_images = _scan_outputs()
        current_map = {img["path"]: img["mtime"] for img in all_images}
        new_images = [img for img in all_images if img["path"] not in before or img["mtime"] > before[img["path"]]]
        if new_images:
            return new_images, current_map
        if time.time() >= deadline:
            return [], current_map
        time.sleep(poll_interval)


def _tail_file(path: str, max_bytes: int = 8192) -> str:
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
    except Exception as e:
        return f"(error reading log file {path}: {e})"

# -------------------------------------------------------------------
# Action handlers
# -------------------------------------------------------------------

def _handle_ping(): return _ok({"message": "pong"})


def _handle_about():
    return _ok({
        "service": "runpod-comfy-flux-ip",
        "version": "v13",
        "comfy": {"base_url": COMFY_BASE, "dir": COMFY_DIR},
        "paths": {"input_dir": INPUT_DIR, "output_dir": OUTPUT_DIR,
                  "output_scan_dirs": OUTPUT_SCAN_DIRS, "comfy_log_path": COMFY_LOG_PATH},
        "features": {"ping": True, "preflight": True, "generate": True,
                     "ip_adapter": True, "history": True,
                     "list_outputs": True, "list_all_outputs": True,
                     "dump_comfy_log": True},
    })


def _handle_preflight():
    try:
        _ensure_comfy_ready()
        resp = _comfy_get("/system_stats")
        resp.raise_for_status()
        stats = resp.json()
    except Exception as e:
        return _fail(f"preflight failed: {e}")
    return _ok({"system_stats": stats})


def _handle_dump_comfy_log(body):
    payload = body.get("payload") or {}
    lines = int(payload.get("lines") or body.get("lines") or 400)
    primary_path = COMFY_LOG_PATH
    tail = _tail_file(primary_path)

    if tail.startswith("(log file not found"):
        alt_path = None
        try:
            for root, _dirs, files in os.walk(COMFY_DIR):
                if "comfyui.log" in files:
                    alt_path = os.path.join(root, "comfyui.log")
                    break
        except Exception:
            alt_path = None
        target_path = alt_path or primary_path
        if alt_path:
            tail = _tail_file(alt_path)
    else:
        target_path = primary_path

    tail_lines = tail.splitlines()
    if len(tail_lines) > lines:
        tail = "\n".join(tail_lines[-lines:])
    return _ok({"log_tail": tail, "path": target_path})


def _handle_list_all_outputs():
    return _ok({"images": _scan_outputs(), "scan_dirs": OUTPUT_SCAN_DIRS})


def _handle_generate(body: Dict[str, Any]) -> Dict[str, Any]:
    payload = body.get("payload") or {}
    client_id = payload.get("client_id", "imagineworlds")

    # Detect and extract workflow (handle both keys safely)
    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        return _fail("generate requires payload.workflow or payload.prompt")

    wait_seconds = float(payload.get("wait_seconds") or 40.0)
    poll_interval = float(payload.get("poll_interval") or 4.0)

    try:
        _ensure_comfy_ready()
    except Exception as e:
        return _fail(f"generate preflight failed: {e}")

    before_images = _scan_outputs()
    before_map = {img["path"]: img["mtime"] for img in before_images}

    try:
        if isinstance(workflow, dict):
            workflow.pop("client_id", None)
            workflow.pop("prompt", None)  # prevent double wrapping

        req = {"client_id": client_id, "prompt": workflow}
        logger.info("Submitting prompt to ComfyUI (client_id=%s)", client_id)
        resp = _comfy_post("/prompt", json=req, timeout=COMFY_REQUEST_TIMEOUT)
        resp.raise_for_status()
        prompt_response = resp.json()
        prompt_id = prompt_response.get("prompt_id")
    except Exception as e:
        return _fail(f"generate failed: {e}")

    new_images, after_map = _await_new_outputs(before_map, wait_seconds, poll_interval)
    latest_image = sorted(new_images, key=lambda x: x["mtime"])[-1] if new_images else None

    return _ok({
        "prompt_id": prompt_id,
        "prompt_response": prompt_response,
        "wait_info": {"before_count": len(before_map), "after_count": len(after_map),
                      "output_scan_dirs": OUTPUT_SCAN_DIRS,
                      "wait_seconds": wait_seconds, "poll_interval": poll_interval},
        "new_images": new_images,
        "latest_image": latest_image,
    })


def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    inp = (event or {}).get("input") or {}
    action = inp.get("action")
    payload = inp.get("payload")
    body = {"payload": payload} if isinstance(payload, dict) else {}
    logger.info("handler action=%s", action)
    try:
        if action == "ping": return _handle_ping()
        if action == "about": return _handle_about()
        if action == "preflight": return _handle_preflight()
        if action == "dump_comfy_log": return _handle_dump_comfy_log(body)
        if action == "list_all_outputs": return _handle_list_all_outputs()
        if action == "generate": return _handle_generate(body)
        return _fail(f"unknown action: {action}")
    except Exception as e:
        logger.exception("Unhandled exception in handler")
        return _fail(f"unhandled exception: {e}")


runpod.serverless.start({"handler": handler})
