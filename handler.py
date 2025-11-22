import os
import time
import logging
import subprocess
from typing import Any, Dict, Optional

import requests
import runpod

# --------------------
# Basic config / paths
# --------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

COMFY_DIR = os.getenv("COMFY_DIR", "/workspace/ComfyUI")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "python3")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")

INPUT_DIR = os.getenv("INPUT_DIR", "/workspace/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/workspace/ComfyUI/output")

SERVICE_NAME = "runpod-comfy-flux-ip"
SERVICE_VERSION = "v6"

# --------------------
# Logging
# --------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="handler.py %(levelname)s %(asctime)s %(message)s",
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

# --------------------
# Comfy process state
# --------------------

_comfy_proc: Optional[subprocess.Popen] = None
_comfy_started_at: Optional[float] = None


# --------------------
# Comfy process helpers
# --------------------

def _start_comfy() -> None:
    """Start ComfyUI if not already running."""
    global _comfy_proc, _comfy_started_at

    # If we already have a live process, do nothing.
    if _comfy_proc is not None and _comfy_proc.poll() is None:
        return

    os.makedirs(os.path.dirname(COMFY_LOG_PATH), exist_ok=True)
    log_file = open(COMFY_LOG_PATH, "a", buffering=1)

    cmd = [
        COMFY_PYTHON,
        "main.py",
        "--listen",
        "0.0.0.0",
        "--port",
        str(COMFY_PORT),
    ]

    logger.info("Launching ComfyUI: %s (cwd=%s)", " ".join(cmd), COMFY_DIR)
    _comfy_proc = subprocess.Popen(
        cmd,
        cwd=COMFY_DIR,
        stdout=log_file,
        stderr=log_file,
        env={**os.environ},
    )
    _comfy_started_at = time.time()
    logger.info("Spawned ComfyUI pid=%s", _comfy_proc.pid)


def _wait_for_comfy(timeout: float = 180.0, poll_interval: float = 2.0) -> None:
    """
    Poll /system_stats until ComfyUI responds or the process dies / times out.
    Raises RuntimeError with a clear message on failure.
    """
    global _comfy_proc

    start = time.time()
    url = f"{COMFY_BASE}/system_stats"
    last_err: Optional[str] = None

    while True:
        # If the process died, bail out early.
        if _comfy_proc is not None and _comfy_proc.poll() is not None:
            code = _comfy_proc.returncode
            msg = f"ComfyUI exited early with code {code}"
            logger.error(msg)
            raise RuntimeError(msg)

        # Try hitting /system_stats.
        try:
            logger.info("Checking ComfyUI health at %s", url)
            resp = requests.get(url, timeout=5)
            if resp.ok:
                logger.info(
                    "ComfyUI is ready after %.1fs",
                    time.time() - start,
                )
                return
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)

        # Timeout?
        if time.time() - start > timeout:
            msg = (
                f"Timed out waiting for ComfyUI at {url} after {timeout:.0f}s; "
                f"last error={last_err}"
            )
            logger.error(msg)
            raise RuntimeError(msg)

        time.sleep(poll_interval)


def _ensure_comfy_ready() -> None:
    """
    Ensure ComfyUI is running and responding on /system_stats.
    """
    _start_comfy()
    _wait_for_comfy()


def _comfy_get_json(path: str, timeout: float = 30.0) -> Dict[str, Any]:
    _ensure_comfy_ready()
    url = COMFY_BASE + path
    logger.info("GET %s", url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _comfy_post_json(path: str, payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    _ensure_comfy_ready()
    url = COMFY_BASE + path
    logger.info("POST %s", url)
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# --------------------
# Action handlers
# --------------------

def _ok(data: Any = None) -> Dict[str, Any]:
    if data is None:
        return {"ok": True}
    return {"ok": True, "data": data}


def _fail(msg: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    logger.error("Action failed: %s", msg)
    out = {"ok": False, "error": msg}
    if extra:
        out["details"] = extra
    return out


def _handle_ping(_inp: Dict[str, Any]) -> Dict[str, Any]:
    return _ok({"message": "pong"})


def _handle_about(_inp: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(
        {
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "description": "ComfyUI Flux Dev + IP Adapter (XLabs) headless worker for ImagineWorlds",
            "env": {
                "COMFY_HOST": COMFY_HOST,
                "COMFY_PORT": COMFY_PORT,
                "COMFY_DIR": COMFY_DIR,
                "INPUT_DIR": INPUT_DIR,
                "OUTPUT_DIR": OUTPUT_DIR,
            },
        }
    )


def _handle_preflight(_inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure ComfyUI can be started and /system_stats is reachable.
    Return basic stats when available.
    """
    try:
        stats = _comfy_get_json("/system_stats")
        return _ok({"system_stats": stats})
    except Exception as e:
        logger.exception("preflight failed")
        return _fail(f"preflight failed: {e.__class__.__name__}: {e}")


def _handle_features(_inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a summary of key nodes and IP adapter options from /object_info.
    """
    try:
        info = _comfy_get_json("/object_info")
    except Exception as e:
        logger.exception("features failed")
        return _fail(f"features failed: {e.__class__.__name__}: {e}")

    lf = info.get("LoadFluxIPAdapter", {})
    lf_req = (lf.get("input", {}) or {}).get("required", {})
    ip_vals = lf_req.get("ipadatper", [[]])[0]
    clipv_vals = lf_req.get("clip_vision", [[]])[0]

    unet = info.get("UNETLoader", {})
    dclip = info.get("DualCLIPLoader", {})

    summary = {
        "has_LoadFluxIPAdapter": bool(lf),
        "has_UNETLoader": bool(unet),
        "has_DualCLIPLoader": bool(dclip),
        "ip_adapter_choices": ip_vals,
        "clip_vision_choices": clipv_vals,
    }
    return _ok(summary)


def _handle_debug_ip_paths(_inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    More detailed view of the IP adapter + CLIP vision fields from /object_info.
    """
    try:
        info = _comfy_get_json("/object_info")
    except Exception as e:
        logger.exception("debug_ip_paths failed")
        return _fail(f"debug_ip_paths failed: {e.__class__.__name__}: {e}")

    lf = info.get("LoadFluxIPAdapter", {})
    lf_input = lf.get("input", {})
    lf_req = lf_input.get("required", {})

    resp = {
        "raw_LoadFluxIPAdapter": lf,
        "ipadatper_field": lf_req.get("ipadatper"),
        "clip_vision_field": lf_req.get("clip_vision"),
        "env": {
            "INPUT_DIR": INPUT_DIR,
            "OUTPUT_DIR": OUTPUT_DIR,
        },
    }
    return _ok(resp)


def _handle_dump_comfy_log(_inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the tail of /tmp/comfy.log so we can debug serverless issues from your Mac.
    """
    lines = int(_inp.get("lines", 200))
    try:
        with open(COMFY_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = "".join(all_lines[-lines:])
        return _ok({"log_tail": tail})
    except FileNotFoundError:
        return _fail(f"Log file not found at {COMFY_LOG_PATH}")
    except Exception as e:
        logger.exception("dump_comfy_log failed")
        return _fail(f"dump_comfy_log failed: {e.__class__.__name__}: {e}")


def _handle_generate(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic 'generate' entrypoint:
      input: {
        "action": "generate",
        "payload": {
          "workflow": { ... full /prompt body ... }
        }
      }

    We forward 'workflow' directly to /prompt and return the Comfy response.
    """
    payload = inp.get("payload") or {}
    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        return _fail("generate requires payload.workflow (or payload.prompt)")

    # Ensure client_id exists so we can track history.
    if "client_id" not in workflow:
        workflow["client_id"] = "flux-ip-runpod"

    try:
        resp = _comfy_post_json("/prompt", workflow, timeout=60.0)
        return _ok({"prompt_response": resp})
    except Exception as e:
        logger.exception("generate failed")
        return _fail(f"generate failed: {e.__class__.__name__}: {e}")


# --------------------
# Runpod handler
# --------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runpod entrypoint. Expects:
      event = { "input": { "action": "...", ... } }
    Returns:
      { "ok": bool, "data"?: {...}, "error"?: "..." }
    """
    inp = event.get("input") or {}
    action = (inp.get("action") or "").lower()

    logger.info("Received action=%s", action or "<none>")

    try:
        if action == "ping":
            return _handle_ping(inp)
        if action == "about":
            return _handle_about(inp)
        if action == "preflight":
            return _handle_preflight(inp)
        if action == "features":
            return _handle_features(inp)
        if action == "debug_ip_paths":
            return _handle_debug_ip_paths(inp)
        if action == "dump_comfy_log":
            return _handle_dump_comfy_log(inp)
        if action == "generate":
            return _handle_generate(inp)

        return _fail(f"Unknown or missing action: {action!r}")

    except Exception as e:
        # Catch-all so we *always* return a JSON payload to Runpod.
        logger.exception("Unhandled error in handler for action=%s", action)
        return _fail(f"unhandled error: {e.__class__.__name__}: {e}")


# Start the Runpod worker loop.
runpod.worker.start({"handler": handler})
