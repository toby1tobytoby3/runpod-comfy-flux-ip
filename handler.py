import os
import time
import base64
import logging
import subprocess
from typing import Any, Dict, Optional, Tuple, List

import requests
import runpod

# --------------------
# Basic config / paths
# --------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "python3")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")

INPUT_DIR = os.getenv("INPUT_DIR", os.path.join(COMFY_DIR, "input"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", os.path.join(COMFY_DIR, "output"))

SERVICE_NAME = "runpod-comfy-flux-ip"
SERVICE_VERSION = "v9"

# Ensure dirs exist even on cold start
for d in [INPUT_DIR, OUTPUT_DIR]:
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        print(f"Warning: could not create {d}: {e}")

# --------------------
# Logging
# --------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="handler.py %(levelname)s %(asctime)s %(message)s",
)
logger = logging.getLogger(__name__)
logger.info("Loaded %s handler, version=%s", SERVICE_NAME, SERVICE_VERSION)

# --------------------
# Comfy process helpers
# --------------------

_comfy_proc: Optional[subprocess.Popen] = None

def _start_comfy() -> None:
    global _comfy_proc
    if _comfy_proc is not None and _comfy_proc.poll() is None:
        return
    log_file = open(COMFY_LOG_PATH, "a", buffering=1)
    cmd = [COMFY_PYTHON, "main.py", "--listen", "0.0.0.0", "--port", str(COMFY_PORT)]
    _comfy_proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=log_file, stderr=log_file)

def _wait_for_comfy(timeout: float = 120.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/system_stats", timeout=5)
            if r.ok:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("ComfyUI not responding")

def _ensure_comfy_ready():
    _start_comfy()
    _wait_for_comfy()

def _comfy_get_json(path: str, timeout: float = 30.0):
    _ensure_comfy_ready()
    r = requests.get(COMFY_BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _comfy_post_json(path: str, payload: Dict[str, Any], timeout: float = 60.0):
    _ensure_comfy_ready()
    r = requests.post(COMFY_BASE + path, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()

# --------------------
# Helpers
# --------------------

def _ok(data=None): return {"ok": True, "data": data} if data is not None else {"ok": True}
def _fail(msg, extra=None): 
    logger.error("Fail: %s", msg)
    out = {"ok": False, "error": msg}
    if extra: out["details"] = extra
    return out

# --------------------
# Actions
# --------------------

def _handle_ping(_): return _ok({"message": "pong"})

def _handle_about(_): 
    return _ok({
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "env": {
            "COMFY_DIR": COMFY_DIR,
            "INPUT_DIR": INPUT_DIR,
            "OUTPUT_DIR": OUTPUT_DIR
        }
    })

def _handle_preflight(_): 
    try:
        stats = _comfy_get_json("/system_stats")
        return _ok({"system_stats": stats})
    except Exception as e:
        return _fail(f"preflight failed: {e}")

def _handle_dump_comfy_log(inp):
    lines = int(inp.get("lines", 200))
    try:
        with open(COMFY_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-lines:])
        return _ok({"log_tail": tail})
    except Exception as e:
        return _fail(f"log read failed: {e}")

def _handle_generate(inp):
    payload = inp.get("payload") or {}
    wf = payload.get("workflow") or payload.get("prompt")
    if not wf:
        return _fail("missing payload.workflow")
    wf.setdefault("client_id", "flux-ip-runpod")
    try:
        resp = _comfy_post_json("/prompt", wf, timeout=90.0)
        return _ok({"prompt_response": resp})
    except Exception as e:
        return _fail(f"generate failed: {e}")

def _handle_upload_input(inp):
    """
    Accepts base64 data and filename -> writes to /comfyui/input
    {
      "action": "upload_input",
      "filename": "ip_ref.png",
      "data": "data:image/png;base64,...."
    }
    """
    fn = inp.get("filename")
    data = inp.get("data")
    if not fn or not data:
        return _fail("upload_input requires filename + data(base64)")
    try:
        if data.startswith("data:"):
            data = data.split(",", 1)[1]
        path = os.path.join(INPUT_DIR, os.path.basename(fn))
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return _ok({"saved": path})
    except Exception as e:
        return _fail(f"upload_input failed: {e}")

# --------------------
# Dispatcher
# --------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    inp = event.get("input") or {}
    act = (inp.get("action") or "").lower()
    try:
        if act == "ping": return _handle_ping(inp)
        if act == "about": return _handle_about(inp)
        if act == "preflight": return _handle_preflight(inp)
        if act == "dump_comfy_log": return _handle_dump_comfy_log(inp)
        if act == "generate": return _handle_generate(inp)
        if act == "upload_input": return _handle_upload_input(inp)
        return _fail(f"unknown action {act}")
    except Exception as e:
        return _fail(f"unhandled: {e}")

runpod.serverless.start({"handler": handler})
