import os
import time
import json
import logging
import threading
import subprocess
from typing import Any, Dict, Optional, Tuple, List

import requests
import runpod

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_DIR = "/workspace/ComfyUI"
COMFY_BOOT_TIMEOUT = int(os.environ.get("COMFY_BOOT_TIMEOUT", "300"))

INPUT_DIR = os.environ.get("INPUT_DIR", f"{COMFY_DIR}/input")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", f"{COMFY_DIR}/output")
os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[handler] %(asctime)s %(levelname)s %(message)s",
)

# -----------------------------------------------------------------------------
# Comfy process management
# -----------------------------------------------------------------------------
_COMFY_PROC: Optional[subprocess.Popen] = None
_COMFY_LOCK = threading.Lock()


def _is_comfy_alive(timeout: float = 2.0) -> bool:
    try:
        r = requests.get(f"{COMFY_BASE}/system_stats", timeout=timeout)
        return r.ok
    except Exception:
        return False


def _start_comfy() -> None:
    """Start ComfyUI if not already running."""
    global _COMFY_PROC
    if _is_comfy_alive():
        logging.info("ComfyUI already running on %s", COMFY_BASE)
        return

    if not os.path.isdir(COMFY_DIR):
        raise RuntimeError(f"ComfyUI directory not found at {COMFY_DIR}")

    with _COMFY_LOCK:
        if _COMFY_PROC and _COMFY_PROC.poll() is None:
            logging.info("ComfyUI process already spawned (pid %s)", _COMFY_PROC.pid)
            return

        cmd = [
            "python3",
            "main.py",
            "--listen",
            "0.0.0.0",
            "--port",
            str(COMFY_PORT),
        ]
        log_path = "/tmp/comfy.log"
        logging.info("Launching ComfyUI: %s", " ".join(cmd))
        log_f = open(log_path, "ab", buffering=0)
        _COMFY_PROC = subprocess.Popen(
            cmd,
            cwd=COMFY_DIR,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
        logging.info("Spawned ComfyUI pid=%s", _COMFY_PROC.pid)


def _wait_for_comfy(timeout: int = COMFY_BOOT_TIMEOUT) -> None:
    """Wait until ComfyUI is reachable or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        if _is_comfy_alive(timeout=5):
            logging.info("ComfyUI is ready on %s", COMFY_BASE)
            return
        # detect early crash
        if _COMFY_PROC and _COMFY_PROC.poll() is not None:
            raise RuntimeError(
                f"ComfyUI exited early with code {_COMFY_PROC.returncode}. "
                "Check /tmp/comfy.log for details."
            )
        time.sleep(3)
    raise RuntimeError(
        f"Timed out waiting for ComfyUI after {timeout}s; check /tmp/comfy.log"
    )


def _ensure_comfy_ready() -> None:
    """Combined helper used by handler actions."""
    _start_comfy()
    _wait_for_comfy()


# -----------------------------------------------------------------------------
# HTTP wrappers
# -----------------------------------------------------------------------------
def _get_json(path: str, timeout: float = 60.0) -> Dict[str, Any]:
    url = f"{COMFY_BASE}{path if path.startswith('/') else '/' + path}"
    _ensure_comfy_ready()
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post_json(path: str, payload: Dict[str, Any], timeout: float = 300.0) -> Dict[str, Any]:
    url = f"{COMFY_BASE}{path if path.startswith('/') else '/' + path}"
    _ensure_comfy_ready()
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


# -----------------------------------------------------------------------------
# Action handlers
# -----------------------------------------------------------------------------
def _handle_ping(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": {"message": "pong"}}


def _handle_about(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "service": "runpod-comfy-flux-ip",
            "version": "v4",
            "description": "ComfyUI Flux Dev + IP Adapter (XLabs) headless worker for ImagineWorlds",
            "env": {
                "COMFY_HOST": COMFY_HOST,
                "COMFY_PORT": COMFY_PORT,
                "INPUT_DIR": INPUT_DIR,
                "OUTPUT_DIR": OUTPUT_DIR,
            },
        },
    }


def _handle_preflight(_: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_comfy_ready()
    stats = _get_json("/system_stats")
    return {"ok": True, "data": {"system_stats": stats}}


def _handle_features(_: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_comfy_ready()
    info = _get_json("/object_info")

    lf = info.get("LoadFluxIPAdapter", {}).get("input", {}).get("required", {})
    ip_opts = lf.get("ipadatper", [])
    clipv_opts = lf.get("clip_vision", [])
    unet_opts = (
        info.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name", [])
    )
    dclip_in = info.get("DualCLIPLoader", {}).get("input", {})
    dclip_req = dclip_in.get("required", {})
    dclip_opt = dclip_in.get("optional", {})

    return {
        "ok": True,
        "data": {
            "LoadFluxIPAdapter": {"ipadatper": ip_opts, "clip_vision": clipv_opts},
            "UNETLoader": {"unet_name": unet_opts},
            "DualCLIPLoader": {"required": dclip_req, "optional": dclip_opt},
        },
    }


def _handle_debug_ip_paths(_: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_comfy_ready()
    info = _get_json("/object_info")
    lf_req = info.get("LoadFluxIPAdapter", {}).get("input", {}).get("required", {})
    return {
        "ok": True,
        "data": {
            "ipadatper": lf_req.get("ipadatper", []),
            "clip_vision": lf_req.get("clip_vision", []),
        },
    }


def _handle_generate(inp: Dict[str, Any]) -> Dict[str, Any]:
    payload = inp.get("payload") or {}
    workflow = payload.get("workflow")
    if not workflow:
        return {"ok": False, "error": "Missing 'payload.workflow'"}

    _ensure_comfy_ready()
    resp = _post_json("/prompt", workflow, timeout=60)
    pid = resp.get("prompt_id")
    if not pid:
        return {"ok": False, "error": "ComfyUI did not return prompt_id"}

    # Poll history
    start = time.time()
    while time.time() - start < 600:
        h = _get_json(f"/history/{pid}")
        if isinstance(h, dict) and h:
            return {"ok": True, "data": {"prompt_id": pid, "history": h}}
        time.sleep(5)
    return {"ok": False, "error": "Timed out waiting for history"}


# -----------------------------------------------------------------------------
# Main RunPod entrypoint
# -----------------------------------------------------------------------------
def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    inp = event.get("input") or {}
    action = inp.get("action", "generate")

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
        if action == "generate":
            return _handle_generate(inp)
        return {"ok": False, "error": f"Unknown action '{action}'"}
    except Exception as e:
        logging.exception("Handler error: %s", e)
        return {"ok": False, "error": str(e)}


runpod.serverless.start({"handler": handler})
