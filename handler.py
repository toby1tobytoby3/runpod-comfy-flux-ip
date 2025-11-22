import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
import runpod

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

INPUT_DIR = os.getenv("INPUT_DIR", "/workspace/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/workspace/ComfyUI/output")

# How long we wait for Comfy /history before giving up
HISTORY_TIMEOUT = int(os.getenv("HISTORY_TIMEOUT", "600"))
HISTORY_POLL_INTERVAL = float(os.getenv("HISTORY_POLL_INTERVAL", "1.0"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

SERVICE_NAME = "runpod-comfy-flux-ip"
HANDLER_VERSION = "v5"

# Nodes we *expect* for the Flux+IP workflow you tested in the pod
REQUIRED_NODES = {
    "LoadFluxIPAdapter",
    "LoadImage",
    "UNETLoader",
    "VAELoader",
    "DualCLIPLoader",
    "CLIPTextEncodeFlux",
    "ConditioningZeroOut",
    "EmptySD3LatentImage",
    "KSampler",
    "VAEDecode",
    "SaveImage",
}

DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[-\w.+/]+);base64,(?P<b64>[A-Za-z0-9+/=]+)$"
)

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s:%(lineno)-4d %(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("handler")


# ---------------------------------------------------------------------------
# Small helpers for standardised responses
# ---------------------------------------------------------------------------

def _ok(data: Any = None) -> Dict[str, Any]:
    """
    Standard success envelope. RunPod will put this under "output".
    """
    return {"ok": True, "data": data}


def _err(
    message: str,
    *,
    error_type: str = "runtime_error",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Standard error envelope. RunPod will put this under "output" *unless*
    the error is thrown at the top-level in which case RunPod may also put
    a string in the top-level "error" field.
    """
    payload: Dict[str, Any] = {
        "type": error_type,
        "message": message,
    }
    if extra is not None:
        payload["extra_info"] = extra
    return {"ok": False, "error": payload}


# ---------------------------------------------------------------------------
# HTTP helpers (we now assume ComfyUI is started by the base image)
# ---------------------------------------------------------------------------

def _comfy_get_json(path: str, *, timeout: int = REQUEST_TIMEOUT) -> Dict[str, Any]:
    url = f"{COMFY_BASE}{path}"
    logger.info("GET %s", url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _comfy_post_json(
    path: str,
    payload: Dict[str, Any],
    *,
    timeout: int = REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    url = f"{COMFY_BASE}{path}"
    logger.info("POST %s", url)
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Comfy inspection helpers
# ---------------------------------------------------------------------------

def _object_info() -> Dict[str, Any]:
    """
    Thin wrapper around /object_info.
    """
    return _comfy_get_json("/object_info")


def _features_from_object_info(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse /object_info to work out what Flux + IP bits are available.
    Returns *data only*, not an _ok/_err envelope.
    """
    nodes = obj or {}
    available_names = set(nodes.keys())

    result: Dict[str, Any] = {
        "available_node_count": len(available_names),
        "missing_required_nodes": [],
        "has_flux": False,
        "has_flux_ip_adapter": False,
        "flux_unets": [],
        "ip_adapters": [],
        "clip_vision_models": [],
        "dual_clip_clip1": [],
        "dual_clip_clip2": [],
    }

    # Missing nodes
    missing = sorted(n for n in REQUIRED_NODES if n not in available_names)
    result["missing_required_nodes"] = missing

    # UNETLoader -> flux models
    unet_info = nodes.get("UNETLoader", {})
    unet_req = (
        unet_info.get("input", {})
        .get("required", {})
        .get("unet_name", [[]])
    )
    unet_opts: List[str] = unet_req[0] if unet_req else []
    flux_unets = sorted([n for n in unet_opts if "flux" in n.lower()])
    result["flux_unets"] = flux_unets
    result["has_flux"] = bool(flux_unets)

    # LoadFluxIPAdapter -> ip_adapter & clip_vision options
    ip_node = nodes.get("LoadFluxIPAdapter", {})
    ip_req = ip_node.get("input", {}).get("required", {})
    ip_adapters = ip_req.get("ipadatper", [[]])[0] if ip_req else []
    clip_visions = ip_req.get("clip_vision", [[]])[0] if ip_req else []

    result["ip_adapters"] = ip_adapters
    result["clip_vision_models"] = clip_visions
    result["has_flux_ip_adapter"] = bool(ip_adapters and clip_visions)

    # DualCLIPLoader -> CLIP options
    dual = nodes.get("DualCLIPLoader", {})
    dual_req = dual.get("input", {}).get("required", {})
    clip1 = dual_req.get("clip_name1", [[]])[0] if dual_req else []
    clip2 = dual_req.get("clip_name2", [[]])[0] if dual_req else []
    result["dual_clip_clip1"] = clip1
    result["dual_clip_clip2"] = clip2

    return result


def _preflight_from_object_info(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Light-weight preflight summary based on /object_info.
    """
    nodes = obj or {}
    available = set(nodes.keys())
    missing = sorted([n for n in REQUIRED_NODES if n not in available])
    return {
        "available_count": len(available),
        "missing": missing,
        "all_good": len(missing) == 0,
    }


# ---------------------------------------------------------------------------
# Upload helper (for IP reference images)
# ---------------------------------------------------------------------------

def _save_data_uri(data_uri: str, out_path: str) -> str:
    """
    Accept either a full data URI ("data:image/png;base64,...") or a plain
    base64 string. Writes to out_path and returns the absolute path.
    """
    m = DATA_URI_RE.match(data_uri.strip())
    if m:
        b64 = m.group("b64")
    else:
        # Assume it's raw base64
        b64 = data_uri.strip()

    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        raise ValueError(f"Failed to decode base64 data: {e}") from e

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(raw)

    return os.path.abspath(out_path)


# ---------------------------------------------------------------------------
# Workflow helpers
# ---------------------------------------------------------------------------

def _validate_minimal_graph(workflow: Dict[str, Any]) -> List[str]:
    """
    Very lightweight validation of a Comfy graph. Returns a list of issues.
    Empty list == looks OK enough to send to Comfy.
    """
    issues: List[str] = []

    if not isinstance(workflow, dict):
        issues.append("Workflow must be a dict of nodes.")
        return issues

    if not workflow:
        issues.append("Workflow is empty.")
        return issues

    # Basic: ensure there's at least one SaveImage in the graph
    has_save = any(
        isinstance(node, dict)
        and node.get("class_type") == "SaveImage"
        for node in workflow.values()
    )
    if not has_save:
        issues.append("No SaveImage node found in workflow.")

    return issues


def _poll_history(prompt_id: str) -> Optional[Dict[str, Any]]:
    """
    Poll /history/{prompt_id} until outputs are available or timeout.
    Returns the entry for that prompt_id (the inner dict), or None if timed out.
    """
    deadline = time.time() + HISTORY_TIMEOUT
    last_payload: Optional[Dict[str, Any]] = None
    path = f"/history/{prompt_id}"

    while time.time() < deadline:
        try:
            payload = _comfy_get_json(path, timeout=REQUEST_TIMEOUT)
        except requests.HTTPError as e:
            # 404 while history isn't yet created is normal; just retry
            if e.response is not None and e.response.status_code == 404:
                time.sleep(HISTORY_POLL_INTERVAL)
                continue
            logger.exception("HTTP error reading history for %s", prompt_id)
            raise
        except requests.RequestException:
            logger.warning("Transient error reading history %s; retrying", prompt_id)
            time.sleep(HISTORY_POLL_INTERVAL)
            continue

        last_payload = payload

        if not isinstance(payload, dict) or prompt_id not in payload:
            time.sleep(HISTORY_POLL_INTERVAL)
            continue

        entry = payload[prompt_id]

        status = entry.get("status")
        if status == "error":
            raise RuntimeError(
                f"Comfy history reported error: {entry.get('error', 'unknown')}"
            )

        outputs = entry.get("outputs")
        if outputs:
            return entry

        time.sleep(HISTORY_POLL_INTERVAL)

    logger.warning(
        "Timed out waiting for history %s; last_payload=%s",
        prompt_id,
        json.dumps(last_payload, default=str) if last_payload else "None",
    )
    return None


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _handle_ping(_: Dict[str, Any]) -> Dict[str, Any]:
    return _ok({"message": "pong"})


def _handle_about(_: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(
        {
            "service": SERVICE_NAME,
            "version": HANDLER_VERSION,
            "description": "ComfyUI Flux Dev + IP Adapter (XLabs) headless worker for ImagineWorlds",
            "env": {
                "COMFY_HOST": COMFY_HOST,
                "COMFY_PORT": COMFY_PORT,
                "INPUT_DIR": INPUT_DIR,
                "OUTPUT_DIR": OUTPUT_DIR,
            },
        }
    )


def _handle_preflight(_: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check that Comfy is reachable and give some basic stats + node status.
    """
    try:
        stats = _comfy_get_json("/system_stats", timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.exception("Failed to call /system_stats")
        return _err(
            f"Failed to call /system_stats: {e}",
            error_type="comfy_unreachable",
        )

    try:
        obj_info = _object_info()
        node_status = _preflight_from_object_info(obj_info)
    except Exception as e:
        logger.exception("Failed to inspect /object_info in preflight")
        return _err(
            f"Failed to inspect /object_info: {e}",
            error_type="object_info_error",
        )

    data = {
        "system_stats": stats,
        "nodes": node_status,
    }
    return _ok(data)


def _handle_features(_: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a snapshot of Flux/IP capabilities (models, adapters, etc).
    """
    try:
        oi = _object_info()
    except Exception as e:
        logger.exception("Failed to fetch object_info")
        return _err(
            f"Failed to fetch object_info: {e}",
            error_type="object_info_error",
        )

    features = _features_from_object_info(oi)
    return _ok(features)


def _handle_debug_ip_paths(_: Dict[str, Any]) -> Dict[str, Any]:
    """
    More detailed view of IP Adapter/vision options to help you debug
    mismatches between disk and /object_info.
    """
    try:
        oi = _object_info()
    except Exception as e:
        logger.exception("Failed to query object_info for debug_ip_paths")
        return _err(
            f"Failed to query object_info: {e}",
            error_type="object_info_error",
        )

    nodes = oi.get("LoadFluxIPAdapter", {}).get("input", {}).get("required", {})
    ip_adapters = nodes.get("ipadatper", [[]])[0] if nodes else []
    clip_visions = nodes.get("clip_vision", [[]])[0] if nodes else []

    return _ok(
        {
            "ip_adapters": ip_adapters,
            "clip_vision_models": clip_visions,
        }
    )


def _handle_upload(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Upload an IP reference image via base64/data URI.

    Expected input:
      {
        "action": "upload",
        "filename": "ip_ref.png",          # optional; default ip_ref.png
        "data_uri": "data:image/png;base64,...."  # or raw base64
      }
    """
    filename = inp.get("filename") or "ip_ref.png"
    data_uri = inp.get("data_uri")
    if not data_uri:
        return _err("Missing 'data_uri' for upload", error_type="bad_request")

    out_path = os.path.join(INPUT_DIR, filename)

    try:
        full_path = _save_data_uri(data_uri, out_path)
    except Exception as e:
        logger.exception("Failed to save data URI")
        return _err(
            f"Failed to save data URI: {e}",
            error_type="upload_error",
        )

    return _ok(
        {
            "filename": filename,
            "path": full_path,
            "relative_to_input_dir": os.path.relpath(full_path, INPUT_DIR),
        }
    )


def _extract_workflow_from_input(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accept both of these client shapes:

      A) {"workflow": {...}}
      B) {"payload": {"workflow": {...}}}

    and return the workflow dict or raise ValueError.
    """
    wf = None

    payload = inp.get("payload")
    if isinstance(payload, dict):
        wf = payload.get("workflow")

    if wf is None:
        wf = inp.get("workflow")

    if wf is None:
        raise ValueError("Missing 'workflow' parameter")

    if not isinstance(wf, dict):
        raise ValueError("'workflow' must be an object/dict")

    return wf


def _handle_generate(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Proxy a Comfy prompt, then poll /history until we have outputs.

    Expected input (flexible):

      {
        "action": "generate",
        "workflow": { ... comfy prompt ... }
      }

      or

      {
        "action": "generate",
        "payload": {
          "workflow": { ... comfy prompt ... },
          "validate_only": false
        }
      }
    """
    # Support validate_only either at top-level or under payload
    payload = inp.get("payload") or {}
    validate_only = bool(
        payload.get("validate_only") or inp.get("validate_only") or False
    )

    try:
        wf = _extract_workflow_from_input(inp)
    except ValueError as e:
        return _err(str(e), error_type="bad_request")

    # Optionally validate without sending to Comfy
    issues = _validate_minimal_graph(wf)
    if validate_only:
        return _ok(
            {
                "validated": len(issues) == 0,
                "issues": issues,
            }
        )

    # Wrap into Comfy's prompt shape if needed
    if "prompt" not in wf:
        wf = {
            "client_id": wf.get("client_id", "iw-runpod"),
            "prompt": wf,
        }

    if "client_id" not in wf:
        wf["client_id"] = "iw-runpod"

    try:
        prompt_resp = _comfy_post_json("/prompt", wf, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.exception("Failed to POST /prompt")
        return _err(
            f"Failed to POST /prompt: {e}",
            error_type="comfy_prompt_error",
        )

    prompt_id = prompt_resp.get("prompt_id") or prompt_resp.get("id")
    if not prompt_id:
        return _err(
            "Comfy /prompt did not return a prompt_id",
            error_type="comfy_prompt_error",
            extra={"response": prompt_resp},
        )

    try:
        history_entry = _poll_history(str(prompt_id))
    except Exception as e:
        logger.exception("Error while polling /history for %s", prompt_id)
        return _err(
            f"Error while polling Comfy history: {e}",
            error_type="comfy_history_error",
            extra={"prompt_id": prompt_id},
        )

    if history_entry is None:
        return _err(
            "Timed out waiting for Comfy history outputs.",
            error_type="timeout",
            extra={"prompt_id": prompt_id},
        )

    return _ok(
        {
            "prompt_id": prompt_id,
            "history": history_entry,
        }
    )


# ---------------------------------------------------------------------------
# RunPod entrypoint
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod serverless entrypoint.

    Expects: event = { "input": { ... } }

    Supported actions:
      - ping
      - about
      - preflight
      - features
      - debug_ip_paths
      - upload
      - generate   (default if no action is provided)
    """
    inp = event.get("input") or {}
    action = inp.get("action") or "generate"

    logger.info("Received action=%s", action)

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
        if action == "upload":
            return _handle_upload(inp)
        if action == "generate":
            return _handle_generate(inp)

        return _err(
            f"Unknown action '{action}'",
            error_type="bad_request",
            extra={"allowed": [
                "ping",
                "about",
                "preflight",
                "features",
                "debug_ip_paths",
                "upload",
                "generate",
            ]},
        )
    except Exception as e:
        logger.exception("Unhandled error in handler for action=%s", action)
        # Last-resort catch so RunPod always gets a structured error
        return _err(str(e), error_type="runtime_error")


# When running this file directly (e.g. if you ever do `python handler.py`)
# this will start the RunPod worker loop. In the standard RunPod base image
# flow, the image entrypoint handles this for you via RUNPOD_HANDLER_MODULE,
# so this block does nothing.
if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
