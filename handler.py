import importlib, importlib.util, json, logging, os, pathlib, time, subprocess, requests, runpod, uuid
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------
# Env / Config
# ---------------------------------------------------------------------
COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "/opt/venv/bin/python")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")
COMFY_REQUEST_TIMEOUT = float(os.getenv("COMFY_REQUEST_TIMEOUT", "60.0"))
COMFY_OUTPUT_WAIT_SECONDS = float(os.getenv("COMFY_OUTPUT_WAIT_SECONDS", "300"))
COMFY_OUTPUT_POLL_INTERVAL = float(os.getenv("COMFY_OUTPUT_POLL_INTERVAL", "5.0"))

INPUT_DIR = os.getenv("INPUT_DIR", "/runpod-volume/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/runpod-volume/ComfyUI/output")
OUTPUT_SCAN_DIRS = [
    OUTPUT_DIR,
    "/runpod-volume/ComfyUI/output",
    "/comfyui/output",
    "/comfyui/user/output",
    "/workspace/ComfyUI/output",
]
for d in OUTPUT_SCAN_DIRS:
    pathlib.Path(d).mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("handler")

import requests
import runpod

# -----------------------------------------------------------------------------
# Config / Constants
# -----------------------------------------------------------------------------

COMFY_BASE = os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188")
COMFY_TIMEOUT = float(os.environ.get("COMFYUI_TIMEOUT", "600"))
COMFY_POLL_INTERVAL = float(os.environ.get("COMFYUI_POLL_INTERVAL", "2.0"))

OUTPUT_DIRS = [
    "/runpod-volume/ComfyUI/output",
    "/comfyui/output",
]

FLUX_BASE_WORKFLOW_PATH = os.environ.get(
    "FLUX_BASE_WORKFLOW_PATH",
    "/workspace/workflows/flux_base_api.json",
)

FLUX_IP_WORKFLOW_PATH = os.environ.get(
    "FLUX_IP_WORKFLOW_PATH",
    "/workspace/workflows/flux_ip_api.json",
)

SAVE_ONLY_WORKFLOW_PATH = os.environ.get(
    "SAVE_ONLY_WORKFLOW_PATH",
    "/workspace/workflows/save_only_test.json",
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("handler")

# -----------------------------------------------------------------------------
# Helpers: Filesystem / Outputs
# -----------------------------------------------------------------------------

def _list_images() -> List[Dict[str, Any]]:
    """Return all PNG images in OUTPUT_DIRS with mtime + size."""
    images: List[Dict[str, Any]] = []
    for base in OUTPUT_DIRS:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if not name.lower().endswith(".png"):
                continue
            full = os.path.join(base, name)
            try:
                st = os.stat(full)
            except FileNotFoundError:
                continue
            images.append(
                {
                    "name": name,
                    "path": full,
                    "mtime": int(st.st_mtime),
                    "size": st.st_size,
                }
            )
    return images


def _snapshot_outputs() -> Dict[str, Dict[str, Any]]:
    """
    Take a snapshot of images keyed by absolute path.
    Used to detect new images after a Comfy prompt.
    """
    snap = {}
    for img in _list_images():
        snap[img["path"]] = img
    return snap


def _diff_outputs(
    before: Dict[str, Dict[str, Any]],
    after: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Return images that are new (or modified) between two snapshots.
    """
    new_images: List[Dict[str, Any]] = []
    for path, meta in after.items():
        if path not in before:
            new_images.append(meta)
            continue
        if meta["mtime"] != before[path]["mtime"] or meta["size"] != before[path]["size"]:
            new_images.append(meta)
    return new_images

# -----------------------------------------------------------------------------
# Helpers: HTTP / ComfyUI
# -----------------------------------------------------------------------------

def _comfy_get(path: str, **kwargs) -> requests.Response:
    url = f"{COMFY_BASE.rstrip('/')}{path}"
    log.info(f"GET {url}")
    return requests.get(url, timeout=COMFY_TIMEOUT, **kwargs)


def _comfy_post(path: str, json: Dict[str, Any], **kwargs) -> requests.Response:
    url = f"{COMFY_BASE.rstrip('/')}{path}"
    log.info(f"POST {url}")
    return requests.post(url, json=json, timeout=COMFY_TIMEOUT, **kwargs)

# -----------------------------------------------------------------------------
# Workflow Loading
# -----------------------------------------------------------------------------

def _load_workflow(path: str) -> Dict[str, Any]:
    """Load a workflow JSON file from disk."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"workflow not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# -----------------------------------------------------------------------------
# Core Actions
# -----------------------------------------------------------------------------

def _handle_ping(body: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": {"message": "pong"}}


def _handle_about(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a small description of what this worker does and where it stores things.
    """
    return {
        "ok": True,
        "data": {
            "description": "ImagineWorlds × RunPod ComfyUI FLUX worker with IP-Adapter.",
            "comfy_base": COMFY_BASE,
            "output_dirs": OUTPUT_DIRS,
            "workflows": {
                "flux_base": FLUX_BASE_WORKFLOW_PATH,
                "flux_ip": FLUX_IP_WORKFLOW_PATH,
                "save_only": SAVE_ONLY_WORKFLOW_PATH,
            },
        },
    }


def _handle_list_outputs(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the list of all current PNG outputs in the configured output dirs.
    """
    images = sorted(_list_images(), key=lambda x: x["mtime"])
    return {"ok": True, "data": {"images": images}}


def _ensure_comfy_ready() -> None:
    """
    Hit /system_stats or /prompt to ensure ComfyUI is up before sending workflows.
    """
    try:
        resp = _comfy_get("/system_stats")
        resp.raise_for_status()
        log.info("ComfyUI /system_stats OK")
        return
    except Exception as e:
        log.warning(f"/system_stats failed: {e}")

    try:
        resp = _comfy_get("/prompt")
        resp.raise_for_status()
        log.info("ComfyUI /prompt OK")
    except Exception as e:
        log.error(f"ComfyUI not ready: {e}")
        raise

# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------

def _await_new_outputs(
    before: Dict[str, Dict[str, Any]],
    timeout: float = COMFY_TIMEOUT,
    poll_interval: float = COMFY_POLL_INTERVAL,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Poll output directories until new images appear or timeout is reached.
    """
    start = time.time()
    while True:
        after = _snapshot_outputs()
        new_images = _diff_outputs(before, after)
        if new_images:
            return new_images, after
        if time.time() - start > timeout:
            log.warning("Timed out waiting for new images.")
            return [], after
        time.sleep(poll_interval)

# -----------------------------------------------------------------------------
# High-Level Generate Handler
# -----------------------------------------------------------------------------

def _build_flux_base_workflow(prompt: str) -> Dict[str, Any]:
    """
    Build a FLUX base workflow payload from the template, injecting the prompt.
    """
    workflow = _load_workflow(FLUX_BASE_WORKFLOW_PATH)
    # Assume the workflow uses a 'client_id' & a text node for the prompt.
    workflow.setdefault("client_id", "flux_client")
    # If your template has a specific node that holds the prompt, update it here.
    # This will depend on your saved workflow. Example:
    # workflow["7"]["inputs"]["text"] = prompt
    # For now, we just store the prompt in a top-level field for debugging.
    workflow["prompt_text"] = prompt
    return workflow


def _build_flux_ip_workflow(prompt: str, ip_image_path: str) -> Dict[str, Any]:
    """
    Build a FLUX + IP-Adapter workflow payload from the template, injecting
    both text prompt and IP adapter image path.
    """
    workflow = _load_workflow(FLUX_IP_WORKFLOW_PATH)
    workflow.setdefault("client_id", "flux_ip_client")

    # Same comment as _build_flux_base_workflow: you will want to set the
    # appropriate text node & IP adapter node inputs to match your template.
    workflow["prompt_text"] = prompt
    workflow["ip_image"] = ip_image_path
    return workflow


def _handle_generate(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle generic 'generate' / 'generate_flux' / 'generate_flux_ip' actions.

    Expected input payload:
    {
        "mode": "flux_base" | "flux_ip",
        "prompt": "text prompt",
        "ip_image_path": "/runpod-volume/ComfyUI/input/ref.png"  # required for flux_ip
    }
    """
    _ensure_comfy_ready()

    mode = body.get("mode", "flux_base")
    prompt = body.get("prompt", "a test image from flux")
    ip_image_path = body.get("ip_image_path")

    if mode not in ("flux_base", "flux_ip"):
        return {
            "ok": False,
            "error": f"unsupported mode: {mode}",
        }

    # Take a snapshot of outputs before we trigger the workflow.
    before = _snapshot_outputs()

    # Build workflow
    if mode == "flux_ip":
        if not ip_image_path:
            return {
                "ok": False,
                "error": "ip_image_path is required for flux_ip mode",
            }
        workflow = _build_flux_ip_workflow(prompt, ip_image_path)
    else:
        workflow = _build_flux_base_workflow(prompt)

    # Force a unique prompt_id every time so Comfy doesn't reuse previous results.
    workflow["prompt_id"] = f"flux_{mode}_{uuid.uuid4()}"

    workflow["prompt_id"] = f"fluxip_{uuid.uuid4()}"

    _ensure_comfy_ready()

    try:
        import flux_double_stream_patch

        importlib.reload(flux_double_stream_patch)
        logger.info("Reloaded flux_double_stream_patch before dispatch.")
    except Exception as e:
        logger.warning("Could not reload flux_double_stream_patch: %s", e)

    before = {i["path"]: i["mtime"] for i in _scan_outputs()}

    # Send workflow to Comfy
    try:
        resp = _comfy_post("/prompt", json=workflow)
        resp.raise_for_status()
        prompt_response = resp.json()
    except Exception as e:
        return {
            "ok": False,
            "error": f"generate failed: {e}",
        }

    # Wait for new images
    new_images, _after = _await_new_outputs(before)
    latest = (
        sorted(new_images, key=lambda x: x["mtime"])[-1]
        if new_images
        else None
    )

    return {
        "ok": True,
        "data": {
            "prompt_response": prompt_response,
            "new_images": new_images,
            "latest_image": latest,
        },
    }

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------

def _handle_preflight(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Do some health checks:
      - can we reach ComfyUI?
      - are the output directories accessible?
      - does the flux_double_stream_patch appear to have loaded?
    """
    status: Dict[str, Any] = {
        "comfy_reachable": False,
        "output_dirs": {},
        "flux_double_stream_patch": {
            "import_failed": False,
            "patched": False,
            "log_path": "/tmp/comfy.log",
            "note": "No explicit success line yet — patch may apply only on first model use.",
        },
        "system_stats": None,
    }

    # Check ComfyUI
    try:
        resp = _comfy_get("/system_stats")
        resp.raise_for_status()
        status["comfy_reachable"] = True
        status["system_stats"] = resp.json()
    except Exception as e:
        status["comfy_reachable"] = False
        status["system_stats"] = {"error": str(e)}

    # Check output dirs
    for d in OUTPUT_DIRS:
        if not os.path.isdir(d):
            status["output_dirs"][d] = {"exists": False, "writable": False}
            continue
        writable = os.access(d, os.W_OK)
        status["output_dirs"][d] = {"exists": True, "writable": writable}

    # Inspect /tmp/comfy.log for our patch signals (best-effort).
    log_path = status["flux_double_stream_patch"]["log_path"]
    if os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-200:]
            tail_text = "".join(tail)
            status["flux_double_stream_patch"]["patched"] = (
                "flux_double_stream_patch" in tail_text
                and "patched DoubleStreamBlock.forward" in tail_text
            )
        except Exception:
            pass

    return {"ok": True, "data": status}

# -----------------------------------------------------------------------------
# RunPod Handler
# -----------------------------------------------------------------------------

def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod serverless handler.

    'event' is the RunPod job payload:
    {
      "id": "...",
      "input": {
         "action": "...",
         ...
      }
    }
    """
    log.info(f"handler received event: {event}")

    body = event.get("input", {}) if isinstance(event, dict) else {}
    if not isinstance(body, dict):
        return {
            "ok": False,
            "error": f"input must be an object, got {type(body)}",
        }

    action = body.get("action")
    if not action:
        return {"ok": False, "error": "missing 'action' in input"}

    if action == "ping":
        return _handle_ping(body)
    if action == "about":
        return _handle_about(body)
    if action == "list_all_outputs":
        return _handle_list_outputs(body)
    if action in ("generate", "generate_flux", "generate_flux_ip"):
        return _handle_generate(body)
    if action == "preflight":
        return _handle_preflight(body)

    return {"ok": False, "error": f"unknown action: {action}"}


runpod.serverless.start({"handler": handler})
