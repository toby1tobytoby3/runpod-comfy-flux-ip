# Serverless handler for Runpod + ComfyUI (headless).
# Supports: ping | about | features | preflight | debug_ip_paths | upload | generate
# - upload: accepts base64 data URIs and writes to /workspace/ComfyUI/input[/<subdir>]
# - generate: optional validate_only; otherwise proxies to ComfyUI /prompt and polls /history
#
# NOTE: Base image must start ComfyUI at 127.0.0.1:8188.

import os, time, json, re, base64, pathlib, typing
from typing import Any, Dict, Tuple
import requests

print(">>> HELLO FROM TOBY'S CUSTOM HANDLER (build v2)", flush=True)


COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

DEFAULT_TIMEOUT = int(os.environ.get("COMFY_TIMEOUT_SECONDS", "600"))
POLL_INTERVAL = float(os.environ.get("COMFY_POLL_INTERVAL", "2.5"))
INPUT_DIR = os.environ.get("COMFY_INPUT_DIR", "/workspace/ComfyUI/input")
OUTPUT_DIR = os.environ.get("COMFY_OUTPUT_DIR", "/workspace/ComfyUI/output")


def _resp(ok: bool, data: Any = None, error: str | None = None) -> Dict[str, Any]:
    return {
        "ok": ok,
        "error": error,
        "data": data,
    }


def _get_json(url: str, timeout: int = 30) -> Dict[str, Any]:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _list_output_images(history: Dict[str, Any]) -> list[Dict[str, Any]]:
    """
    Given the /history response, flatten out all images with their file paths.
    """
    images: list[Dict[str, Any]] = []
    for _prompt_id, entry in history.items():
        outputs = entry.get("outputs") or {}
        for _node_id, node_out in outputs.items():
            imgs = node_out.get("images") or []
            for img in imgs:
                images.append(img)
    return images


def _load_ip_adapter_and_clip_info() -> Dict[str, Any]:
    """
    Query /object_info and return what this pod thinks is valid for LoadFluxIPAdapter.
    Useful for debugging "ip_adapter name not found" issues from the outside.
    """
    info = _get_json(f"{COMFY_BASE}/object_info")
    lf = info.get("LoadFluxIPAdapter") or {}
    req = (lf.get("input") or {}).get("required") or {}
    return {
        "node": lf,
        "ipadatper": req.get("ipadatper"),
        "clip_vision": req.get("clip_vision"),
    }


def _build_features() -> Dict[str, Any]:
    """
    Very lightweight caps that your Supabase / ImagineWorlds side can query.
    """
    try:
        obj = _get_json(f"{COMFY_BASE}/object_info")
    except Exception as e:
        return {
            "ok": False,
            "error": f"Failed to fetch object_info: {e}",
        }

    nodes = set(obj.keys())
    has_flux_ip_adapter = "LoadFluxIPAdapter" in nodes
    has_flux_unet_loader = "UNETLoader" in nodes
    has_flux_clip = "DualCLIPLoader" in nodes

    flux_nodes = {
        "LoadFluxIPAdapter",
        "UNETLoader",
        "EmptySD3LatentImage",
        "CLIPTextEncodeFlux",
        "KSampler",
        "VAEDecode",
        "SaveImage",
        "CLIPLoader",
        "FluxGuidance",
        "DualCLIPLoader",
        "ModelSamplingFlux",  # optional but nice to assert
    }

    return {
        "ok": True,
        "info": {
            "has_flux_ip_adapter": has_flux_ip_adapter,
            "has_flux_unet_loader": has_flux_unet_loader,
            "has_flux_clip": has_flux_clip,
            "missing_flux_nodes": sorted(list(flux_nodes - nodes)),
        },
    }


DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<b64>.+)$")


def _ok_path_under(base_dir: str, rel: str) -> pathlib.Path:
    """
    Ensure we only ever write inside INPUT_DIR (no ../../../ escapes).
    """
    base = pathlib.Path(base_dir).resolve()
    dest = (base / rel).resolve()
    if base not in dest.parents and base != dest:
        raise ValueError("Invalid path (escaping base dir)")
    return dest


def _save_data_uri_to_file(
    data_uri: str,
    base_dir: str,
    rel_path: str,
) -> Dict[str, Any]:
    """
    Accept 'data:image/png;base64,...' and write to base_dir/rel_path.
    Returns relative path from base_dir for Comfy's LoadImage usage.
    """
    m = DATA_URI_RE.match(data_uri)
    if not m:
        raise ValueError("Invalid data URI")

    b64_data = m.group("b64")
    raw = base64.b64decode(b64_data)

    dest = _ok_path_under(base_dir, rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)

    rel = str(dest.relative_to(base_dir))
    return {
        "path": str(dest),
        "relative": rel,
        "size_bytes": len(raw),
    }


def _handle_upload(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    action = "upload"
    input: {
      "action": "upload",
      "files": [
        {
          "data_uri": "data:image/png;base64,...",
          "target": "ip_refs/dragon_01.png"
        },
        ...
      ]
    }
    """
    files = inp.get("files") or []
    if not files:
        return _resp(False, error="No files provided")

    saved: list[Dict[str, Any]] = []
    for idx, file_spec in enumerate(files):
        data_uri = file_spec.get("data_uri")
        target = file_spec.get("target") or f"upload_{idx}.png"
        if not data_uri:
            return _resp(False, error=f"files[{idx}].data_uri missing")

        try:
            info = _save_data_uri_to_file(data_uri, INPUT_DIR, target)
        except Exception as e:
            return _resp(False, error=f"Failed to save files[{idx}]: {e}")
        saved.append(info)

    return _resp(True, data={"saved": saved})


def _handle_ping() -> Dict[str, Any]:
    return _resp(True, data={"message": "pong"})


def _handle_about() -> Dict[str, Any]:
    return _resp(
        True,
        data={
            "service": "runpod-comfy-flux-ip",
            "version": "v2",
            "description": "ComfyUI Flux Dev + IP Adapter (XLabs) headless worker for ImagineWorlds",
            "env": {
                "COMFY_HOST": COMFY_HOST,
                "COMFY_PORT": COMFY_PORT,
                "INPUT_DIR": INPUT_DIR,
                "OUTPUT_DIR": OUTPUT_DIR,
            },
        },
    )


def _handle_preflight() -> Dict[str, Any]:
    """
    Light-weight check that:
      - Comfy /system_stats responds
      - /object_info has key nodes we care about
    """
    try:
        stats = _get_json(f"{COMFY_BASE}/system_stats")
    except Exception as e:
        return _resp(False, error=f"Failed to call /system_stats: {e}")

    features = _build_features()
    return _resp(
        True,
        data={
            "system_stats": stats,
            "features": features,
        },
    )


def _handle_debug_ip_paths() -> Dict[str, Any]:
    try:
        info = _load_ip_adapter_and_clip_info()
        return _resp(True, data=info)
    except Exception as e:
        return _resp(False, error=f"Failed to query object_info: {e}")


def _handle_generate(inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    action = "generate"

    Expected input format (outer JSON from your client):
    {
      "input": {
        "action": "generate",
        "payload": {
          "workflow": { ... comfy prompt json ... },
          "validate_only": false
        }
      }
    }
    """
    payload = inp.get("payload") or {}
    workflow = payload.get("workflow")
    validate_only = bool(payload.get("validate_only", False))

    if not workflow:
        return _resp(False, error="Missing payload.workflow")

    # If you want, you can do local validation here (assert node ids, etc.)
    if validate_only:
        return _resp(True, data={"validated": True})

    prompt_url = f"{COMFY_BASE}/prompt"
    history_url = f"{COMFY_BASE}/history"

    try:
        submit_resp = _post_json(prompt_url, workflow, timeout=30)
    except Exception as e:
        return _resp(False, error=f"Failed to POST /prompt: {e}")

    prompt_id = submit_resp.get("prompt_id")
    if not prompt_id:
        return _resp(False, error=f"/prompt response missing prompt_id: {submit_resp}")

    # Poll /history until done or timeout.
    deadline = time.time() + DEFAULT_TIMEOUT
    last_status: str | None = None
    while True:
        if time.time() > deadline:
            return _resp(
                False,
                error=f"Timed out waiting for history for prompt_id={prompt_id}, last_status={last_status}",
            )

        try:
            h = _get_json(f"{history_url}/{prompt_id}", timeout=30)
        except Exception as e:
            # transient network / comfy restart etc.
            last_status = f"error: {e}"
            time.sleep(POLL_INTERVAL)
            continue

        # Comfy returns {prompt_id: { "status": {...}, "outputs": {...} } }
        entry = h.get(prompt_id)
        if not entry:
            last_status = "missing_entry"
            time.sleep(POLL_INTERVAL)
            continue

        status = (entry.get("status") or {}).get("status")
        last_status = status

        if status in ("success", "error", "failed"):
            # We're done.
            images = _list_output_images(h)
            return _resp(
                status == "success",
                data={
                    "prompt_id": prompt_id,
                    "status": status,
                    "history": h,
                    "images": images,
                },
                error=None if status == "success" else f"Comfy status={status}",
            )

        time.sleep(POLL_INTERVAL)


def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod serverless entrypoint.
    event is expected to be like:
    {
      "input": {
        "action": "ping" | "about" | "features" | "preflight" | "debug_ip_paths" | "upload" | "generate",
        ...
      }
    }
    """
    print(">>> handler(event) invoked", flush=True)
    print(json.dumps({"event_preview": str(event)[:512]}, indent=2), flush=True)

    inp = event.get("input") or {}
    action = inp.get("action") or "generate"

    try:
        if action == "ping":
            return _handle_ping()
        if action == "about":
            return _handle_about()
        if action == "preflight":
            return _handle_preflight()
        if action == "features":
            return _build_features()
        if action == "debug_ip_paths":
            return _handle_debug_ip_paths()
        if action == "upload":
            return _handle_upload(inp)
        if action == "generate":
            return _handle_generate(inp)

        return _resp(False, error=f"Unknown action: {action}")
    except Exception as e:
        # Defensive catch-all so that we always return a JSON object, not explode the container.
        return _resp(False, error=f"Exception in handler: {e}")

# --- RunPod serverless bootstrap ---
import runpod

print("💡 Custom handler.py loaded successfully!")
runpod.serverless.start({"handler": handler})
