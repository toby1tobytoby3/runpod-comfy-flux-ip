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

COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "python3")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")

INPUT_DIR = os.getenv("INPUT_DIR", os.path.join(COMFY_DIR, "input"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", os.path.join(COMFY_DIR, "output"))

SERVICE_NAME = "runpod-comfy-flux-ip"
SERVICE_VERSION = "v10"

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
    """Ensure ComfyUI is started and healthy."""
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
# Small helpers
# -------------

def _ok(data=None):
    if data is None:
        return {"ok": True}
    return {"ok": True, "data": data}


def _fail(message: str, extra: Optional[Dict[str, Any]] = None):
    logger.error("Fail: %s", message)
    out: Dict[str, Any] = {"ok": False, "error": message}
    if extra:
        out["details"] = extra
    return out


# ---------------
# Action handlers
# ---------------

def _handle_ping(_: Dict[str, Any]):
    return _ok({"message": "pong"})


def _handle_about(_: Dict[str, Any]):
    return _ok(
        {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "env": {
                "COMFY_DIR": COMFY_DIR,
                "INPUT_DIR": INPUT_DIR,
                "OUTPUT_DIR": OUTPUT_DIR,
            },
        }
    )


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


def _handle_features(_: Dict[str, Any]):
    """
    Return a small summary of capabilities, matching what you've used:
    {
      "clip_vision_choices": [...],
      "ip_adapter_choices": [...],
      "has_DualCLIPLoader": true/false,
      "has_LoadFluxIPAdapter": true/false,
      "has_UNETLoader": true/false
    }
    """
    try:
        obj = _comfy_get_json("/object_info")
    except Exception as e:
        return _fail(f"features failed: {e}")

    data: Dict[str, Any] = {
        "clip_vision_choices": [],
        "ip_adapter_choices": [],
        "has_DualCLIPLoader": "DualCLIPLoader" in obj,
        "has_LoadFluxIPAdapter": "LoadFluxIPAdapter" in obj,
        "has_UNETLoader": "UNETLoader" in obj,
    }

    try:
        lf = obj.get("LoadFluxIPAdapter", {})
        req = (lf.get("input") or {}).get("required") or {}
        ip_opts = (req.get("ipadatper") or {}).get("options", {})
        clip_opts = (req.get("clip_vision") or {}).get("options", {})
        data["ip_adapter_choices"] = ip_opts.get("choices", []) or []
        data["clip_vision_choices"] = clip_opts.get("choices", []) or []
    except Exception as e:
        logger.warning("features: could not parse LoadFluxIPAdapter choices: %s", e)

    return _ok(data)


def _handle_generate(inp: Dict[str, Any]):
    """
    Expects:
    {
      "action": "generate",
      "payload": {
        "workflow": { ... }   # your existing workflow JSON
      }
    }
    """
    # Ensure output dir exists (runtime safety)
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        testfile = os.path.join(OUTPUT_DIR, "writecheck.txt")
        with open(testfile, "w") as f:
            f.write("ok")
        logger.info("Writecheck passed: %s", testfile)
    except Exception as e:
        logger.error("Writecheck failed: %s", e)
    
    payload = inp.get("payload") or {}
    wf = payload.get("workflow") or payload.get("prompt")
    if not wf:
        return _fail("generate requires payload.workflow (or payload.prompt)")

    # Ensure client_id is set
    if "client_id" not in wf:
        wf["client_id"] = "flux-ip-runpod"

    try:
        resp = _comfy_post_json("/prompt", wf, timeout=120.0)
        return _ok({"prompt_response": resp})
    except Exception as e:
        return _fail(f"generate failed: {e}")


def _handle_history(inp: Dict[str, Any]):
    """
    Expects:
    {
      "action": "history",
      "payload": { "prompt_id": "..." }
    }

    Returns:
    {
      "prompt_id": "...",
      "images": [ { "node_id": "...", "image": { ... } }, ... ],
      "raw": { ...full comfy history... },
      "status": "..."
    }
    """
    payload = inp.get("payload") or {}
    prompt_id = payload.get("prompt_id") or inp.get("prompt_id")
    if not prompt_id:
        return _fail("history requires payload.prompt_id")

    try:
        hist = _comfy_get_json(f"/history/{prompt_id}")
    except Exception as e:
        return _fail(f"history failed: {e}")

    images: List[Dict[str, Any]] = []
    status = ""

    try:
        h_entry = (hist.get("history") or {}).get(prompt_id) or {}
        outputs = h_entry.get("outputs") or {}
        status = h_entry.get("status", "")

        for node_id, outs in outputs.items():
            for out in outs:
                for img in out.get("images", []):
                    images.append(
                        {
                            "node_id": node_id,
                            "image": img,  # contains 'filename', 'subfolder', 'type', etc.
                        }
                    )
    except Exception as e:
        logger.warning("history: failed to parse images from history: %s", e)

    return _ok(
        {
            "prompt_id": prompt_id,
            "images": images,
            "raw": hist,
            "status": status,
        }
    )


def _handle_upload_input(inp: Dict[str, Any]):
    """
    Upload raw/base64 image data to /comfyui/input.

    Input:
    {
      "action": "upload_input",
      "filename": "ip_ref.png",
      "data": "data:image/png;base64,...." OR pure base64
    }
    """
    filename = inp.get("filename")
    data = inp.get("data")
    if not filename or not data:
        return _fail("upload_input requires filename and data")

    try:
        if data.startswith("data:"):
            data = data.split(",", 1)[1]

        dest = os.path.join(INPUT_DIR, os.path.basename(filename))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(base64.b64decode(data))

        logger.info("upload_input: wrote %s", dest)
        return _ok({"saved": dest})
    except Exception as e:
        return _fail(f"upload_input failed: {e}")


def _handle_upload_input_url(inp: Dict[str, Any]):
    """
    Download an image from a URL and save to /comfyui/input.

    Input:
    {
      "action": "upload_input_url",
      "url": "https://public.supabase.../ip_ref.png",
      "filename": "ip_ref.png"      # optional; default = last path segment
    }
    """
    url = inp.get("url")
    if not url:
        return _fail("upload_input_url requires url")

    filename = inp.get("filename")
    if not filename:
        # derive from URL path
        filename = url.split("?")[0].rstrip("/").split("/")[-1] or "ip_ref.png"

    try:
        logger.info("upload_input_url: downloading %s", url)
        r = requests.get(url, timeout=30)
        r.raise_for_status()

        dest = os.path.join(INPUT_DIR, os.path.basename(filename))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(r.content)

        logger.info("upload_input_url: saved %s (%d bytes)", dest, len(r.content))
        return _ok({"saved": dest})
    except Exception as e:
        return _fail(f"upload_input_url failed: {e}")


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
        if action == "features":
            return _handle_features(inp)
        if action == "generate":
            return _handle_generate(inp)
        if action == "history":
            return _handle_history(inp)
        if action == "upload_input":
            return _handle_upload_input(inp)
        if action == "upload_input_url":
            return _handle_upload_input_url(inp)

        return _fail(f"unknown action '{action}'")

    except Exception as e:
        return _fail(f"unhandled exception: {e}")


runpod.serverless.start({"handler": handler})
