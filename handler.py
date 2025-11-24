import os
import time
import base64
import logging
import subprocess
from typing import Any, Dict, Optional, List
import requests
import runpod

# --------------------------------
# Basic config / paths / constants
# --------------------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

COMFY_DIR = os.getenv("COMFY_DIR", "/workspace/ComfyUI")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "python3")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")

# ✅ Updated to match ComfyUI’s true runtime paths
INPUT_DIR = os.getenv("INPUT_DIR", "/workspace/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/workspace/ComfyUI/output")

SERVICE_NAME = "runpod-comfy-flux-ip"
SERVICE_VERSION = "v11"

# Ensure input/output dirs exist on cold start
for d in (INPUT_DIR, OUTPUT_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        print(f"[WARN] could not create {d}: {e}")

# -------------
# Logging setup
# -------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="handler.py:%(lineno)d  %(asctime)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)
logger.info("Loaded %s handler, version=%s", SERVICE_NAME, SERVICE_VERSION)
logger.info(
    "COMFY_BASE=%s COMFY_DIR=%s INPUT_DIR=%s OUTPUT_DIR=%s",
    COMFY_BASE,
    COMFY_DIR,
    INPUT_DIR,
    OUTPUT_DIR,
)

# ------------------------
# Comfy process management
# ------------------------

_comfy_proc: Optional[subprocess.Popen] = None


def _start_comfy() -> None:
    """Start ComfyUI process if not already running."""
    global _comfy_proc
    if _comfy_proc is not None and _comfy_proc.poll() is None:
        return

    logger.info("Launching ComfyUI: %s main.py --listen 0.0.0.0 --port %s", COMFY_PYTHON, COMFY_PORT)
    log_file = open(COMFY_LOG_PATH, "a", buffering=1)
    cmd = [COMFY_PYTHON, "main.py", "--listen", "0.0.0.0", "--port", str(COMFY_PORT)]
    _comfy_proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=log_file, stderr=log_file)
    logger.info("Spawned ComfyUI pid=%s", _comfy_proc.pid)


def _wait_for_comfy(timeout: float = 120.0) -> None:
    """Wait until ComfyUI /system_stats is responsive."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_BASE}/system_stats", timeout=5)
            if r.ok:
                logger.info("ComfyUI /system_stats OK")
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("ComfyUI not responding within timeout")


def _ensure_comfy_ready():
    _start_comfy()
    _wait_for_comfy()


def _comfy_get_json(path: str, timeout: float = 30.0):
    _ensure_comfy_ready()
    url = COMFY_BASE + path
    logger.info("GET %s", url)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _comfy_post_json(path: str, payload: Dict[str, Any], timeout: float = 60.0):
    _ensure_comfy_ready()
    url = COMFY_BASE + path
    logger.info("POST %s", url)
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


# -------------
# Helpers
# -------------

def _ok(data=None):
    return {"ok": True, "data": data} if data is not None else {"ok": True}


def _fail(msg: str, extra: Optional[Dict[str, Any]] = None):
    logger.error("Fail: %s", msg)
    out = {"ok": False, "error": msg}
    if extra:
        out["details"] = extra
    return out


# ---------------
# Action handlers
# ---------------

def _handle_ping(_: Dict[str, Any]):
    return _ok({"message": "pong"})


def _handle_about(_: Dict[str, Any]):
    return _ok({
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "env": {
            "COMFY_DIR": COMFY_DIR,
            "INPUT_DIR": INPUT_DIR,
            "OUTPUT_DIR": OUTPUT_DIR
        },
    })


def _handle_preflight(_: Dict[str, Any]):
    try:
        stats = _comfy_get_json("/system_stats")
        return _ok({"system_stats": stats})
    except Exception as e:
        return _fail(f"preflight failed: {e}")


def _handle_dump_comfy_log(inp: Dict[str, Any]):
    lines = int(inp.get("lines", 200))
    try:
        with open(COMFY_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-lines:])
        return _ok({"log_tail": tail})
    except Exception as e:
        return _fail(f"log read failed: {e}")


def _handle_upload_input_url(inp: Dict[str, Any]):
    url = inp.get("url")
    if not url:
        return _fail("upload_input_url requires url")

    filename = inp.get("filename") or url.split("?")[0].split("/")[-1]
    try:
        logger.info("Downloading %s", url)
        r = requests.get(url, timeout=30)
        r.raise_for_status()

        dest = os.path.join(INPUT_DIR, os.path.basename(filename))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(r.content)

        logger.info("Saved to %s (%d bytes)", dest, len(r.content))
        return _ok({"saved": dest})
    except Exception as e:
        return _fail(f"upload_input_url failed: {e}")


def _handle_generate(inp: Dict[str, Any]):
    # Ensure output dir is writable
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        test = os.path.join(OUTPUT_DIR, "writecheck.txt")
        with open(test, "w") as f:
            f.write("ok")
        logger.info("Writecheck passed: %s", test)
    except Exception as e:
        logger.error("Writecheck failed: %s", e)

    payload = inp.get("payload", {})
    wf = payload.get("workflow") or payload.get("prompt")
    if not wf:
        return _fail("generate requires payload.workflow")

    wf.setdefault("client_id", "flux-ip-runpod")

    try:
        resp = _comfy_post_json("/prompt", wf, timeout=120.0)
        return _ok({"prompt_response": resp})
    except Exception as e:
        return _fail(f"generate failed: {e}")


# -----------
# Dispatcher
# -----------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    inp = event.get("input") or {}
    action = (inp.get("action") or "").lower()
    logger.info("Received action=%s", action)

    try:
        if action == "ping":
            return _handle_ping(inp)
        if action == "about":
            return _handle_about(inp)
        if action == "preflight":
            return _handle_preflight(inp)
        if action == "dump_comfy_log":
            return _handle_dump_comfy_log(inp)
        if action == "upload_input_url":
            return _handle_upload_input_url(inp)
        if action == "generate":
            return _handle_generate(inp)
        return _fail(f"unknown action '{action}'")
    except Exception as e:
        return _fail(f"unhandled exception: {e}")


runpod.serverless.start({"handler": handler})
