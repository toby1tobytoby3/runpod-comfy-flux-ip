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
# Basic config
# -----------------------------------------------------------------------------

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))

INPUT_DIR = os.environ.get("INPUT_DIR", "/workspace/ComfyUI/input")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/workspace/ComfyUI/output")

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[handler] %(asctime)s %(levelname)s %(message)s",
)

# -----------------------------------------------------------------------------
# ComfyUI process management
# -----------------------------------------------------------------------------

_COMFY_PROC: Optional[subprocess.Popen] = None
_COMFY_LOCK = threading.Lock()


def _comfy_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{COMFY_HOST}:{COMFY_PORT}{path}"


def _is_port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _start_comfy_if_needed() -> None:
    """
    Ensure ComfyUI is running inside the container.

    Our Dockerfile's CMD is now `python3 /workspace/handler.py`, so the base
    image's default entrypoint that would normally start Comfy is bypassed.
    We therefore spawn ComfyUI ourselves (once) and then use HTTP against it.
    """
    global _COMFY_PROC

    with _COMFY_LOCK:
        # If port already accepting connections, we assume Comfy is up.
        if _is_port_open(COMFY_HOST, COMFY_PORT):
            return

        # If we have a process and it's still alive, just return; it may still
        # be starting up and _wait_for_comfy will poll.
        if _COMFY_PROC is not None and _COMFY_PROC.poll() is None:
            logging.info("ComfyUI process already spawned, waiting for readiness.")
            return

        # Spawn ComfyUI
        cmd = [
            "python3",
            "main.py",
            "--listen",
            "0.0.0.0",
            "--port",
            str(COMFY_PORT),
        ]
        logging.info("Starting ComfyUI: %s", " ".join(cmd))

        # Log to /tmp/comfy.log so we can inspect via the RunPod logs if needed.
        log_path = "/tmp/comfy.log"
        try:
            log_f = open(log_path, "ab")  # type: ignore[assignment]
        except Exception:
            log_f = subprocess.DEVNULL  # type: ignore[assignment]

        try:
            _COMFY_PROC = subprocess.Popen(
                cmd,
                cwd="/workspace/ComfyUI",
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            logging.exception("Failed to spawn ComfyUI: %s", e)
            raise


def _wait_for_comfy(timeout: int = 120) -> None:
    """
    Wait until ComfyUI responds on /system_stats or timeout.
    """
    start = time.time()
    last_err: Optional[Exception] = None

    while True:
        try:
            r = requests.get(_comfy_url("/system_stats"), timeout=5)
            if r.status_code == 200:
                logging.info("ComfyUI is ready.")
                return
        except Exception as e:  # noqa: BLE001
            last_err = e

        if time.time() - start > timeout:
            logging.error("Timed out waiting for ComfyUI to start.")
            raise RuntimeError(f"Timed out waiting for ComfyUI: {last_err}")

        time.sleep(2)


def _ensure_comfy_ready() -> None:
    """
    Public helper: start Comfy if needed and wait for readiness.
    Use this before any call to /system_stats, /object_info, /prompt, etc.
    """
    _start_comfy_if_needed()
    _wait_for_comfy()


# -----------------------------------------------------------------------------
# HTTP helpers to ComfyUI
# -----------------------------------------------------------------------------

def _comfy_get(path: str, timeout: int = 30) -> requests.Response:
    url = _comfy_url(path)
    logging.info("GET %s", url)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def _comfy_post_json(path: str, payload: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
    url = _comfy_url(path)
    logging.info("POST %s", url)
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
            "version": "v3",
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
    """
    Check that ComfyUI is up and return basic system stats.
    """
    _ensure_comfy_ready()
    stats = _comfy_get("/system_stats").json()
    return {
        "ok": True,
        "data": {
            "system_stats": stats,
        },
    }


def _handle_features(_: Dict[str, Any]) -> Dict[str, Any]:
    """
    Summarise key ComfyUI capabilities, especially Flux + IP adapter bits.
    """
    _ensure_comfy_ready()
    info = _comfy_get("/object_info").json()

    load_flux = info.get("LoadFluxIPAdapter", {})
    lf_in = (load_flux.get("input") or {}).get("required", {})

    ip_opts = lf_in.get("ipadatper") or []
    clipv_opts = lf_in.get("clip_vision") or []

    unet_req = (
        info.get("UNETLoader", {})
        .get("input", {})
        .get("required", {})
        .get("unet_name", [])
    )

    dclip_input = info.get("DualCLIPLoader", {}).get("input", {})
    dclip_req = dclip_input.get("required", {})
    dclip_opt = dclip_input.get("optional", {})

    return {
        "ok": True,
        "data": {
            "LoadFluxIPAdapter": {
                "ipadatper": ip_opts,
                "clip_vision": clipv_opts,
            },
            "UNETLoader": {
                "unet_name": unet_req,
            },
            "DualCLIPLoader": {
                "required": dclip_req,
                "optional": dclip_opt,
            },
        },
    }


def _handle_debug_ip_paths(_: Dict[str, Any]) -> Dict[str, Any]:
    """
    Specifically dump the IP adapter + CLIP vision choices from object_info.
    """
    _ensure_comfy_ready()
    info = _comfy_get("/object_info").json()

    lf_req = (
        info.get("LoadFluxIPAdapter", {})
        .get("input", {})
        .get("required", {})
    )

    ip_vals = lf_req.get("ipadatper", [])
    clipv_vals = lf_req.get("clip_vision", [])

    return {
        "ok": True,
        "data": {
            "ipadatper": ip_vals,
            "clip_vision": clipv_vals,
        },
    }


def _extract_images_from_history(history: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Given /history response, pull out image filenames + subfolders for convenience.
    """
    if not history:
        return []

    # history is {prompt_id: {...}}
    _, record = next(iter(history.items()))
    outputs = record.get("outputs") or {}

    images: List[Dict[str, Any]] = []
    for node_id, node_out in outputs.items():
        imgs = node_out.get("images") or []
        for img in imgs:
            filename = img.get("filename")
            subfolder = img.get("subfolder", "")
            img_type = img.get("type", "output")
            if not filename:
                continue
            full_path = os.path.join(OUTPUT_DIR, subfolder, filename)
            images.append(
                {
                    "node": node_id,
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": img_type,
                    "path": full_path,
                }
            )
    return images


def _wait_for_history(prompt_id: str, timeout: int = 600, poll_interval: float = 5.0) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Poll /history/{prompt_id} until outputs appear or we time out.
    """
    start = time.time()
    last_history: Optional[Dict[str, Any]] = None

    while True:
        try:
            h = _comfy_get(f"/history/{prompt_id}", timeout=30).json()
            last_history = h
            images = _extract_images_from_history(h)
            if images:
                return h, images
        except Exception as e:  # noqa: BLE001
            logging.warning("Error polling history for %s: %s", prompt_id, e)

        if time.time() - start > timeout:
            logging.error("Timed out waiting for history for %s", prompt_id)
            return last_history, _extract_images_from_history(last_history or {})

        time.sleep(poll_interval)


def _handle_generate(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run a full Flux+IP workflow via /prompt and wait for images to land in history.
    Expects:
        {
          "action": "generate",
          "payload": {
            "workflow": { ... }   # standard ComfyUI workflow JSON
          }
        }
    """
    payload = inp.get("payload") or {}
    workflow = payload.get("workflow")

    if not workflow:
        return {
            "ok": False,
            "error": "Missing 'payload.workflow' in input",
        }

    _ensure_comfy_ready()

    # POST workflow to /prompt
    try:
        prompt_resp = _comfy_post_json("/prompt", workflow, timeout=30)
    except Exception as e:  # noqa: BLE001
        logging.exception("Error posting workflow to /prompt: %s", e)
        return {
            "ok": False,
            "error": f"Failed to POST /prompt: {e}",
        }

    prompt_id = prompt_resp.get("prompt_id")
    number = prompt_resp.get("number")

    if not prompt_id:
        return {
            "ok": False,
            "error": f"/prompt did not return prompt_id: {prompt_resp}",
        }

    # Poll /history until images appear (or timeout)
    history, images = _wait_for_history(prompt_id)

    return {
        "ok": True,
        "data": {
            "prompt_response": prompt_resp,
            "prompt_id": prompt_id,
            "number": number,
            "images": images,
            "history": history,
        },
    }


# -----------------------------------------------------------------------------
# Main RunPod handler
# -----------------------------------------------------------------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod serverless handler.

    Expects event like:
      {
        "input": {
          "action": "ping" | "about" | "preflight" | "features" |
                     "debug_ip_paths" | "generate",
          ...
        }
      }
    """
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

        return {
            "ok": False,
            "error": f"Unknown action '{action}'",
        }

    except Exception as e:  # noqa: BLE001
        logging.exception("Unhandled exception in handler: %s", e)
        return {
            "ok": False,
            "error": str(e),
        }


# Required by RunPod serverless runtime
runpod.serverless.start({"handler": handler})
