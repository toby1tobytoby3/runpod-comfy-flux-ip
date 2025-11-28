import importlib.util, json, logging, os, pathlib, time, subprocess, uuid, requests, runpod
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------
# Env / Config
# ---------------------------------------------------------------------

COMFY_BASE = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
COMFY_PY = os.environ.get("COMFY_PY", "/opt/venv/bin/python")
COMFY_MAIN = os.environ.get("COMFY_MAIN", "/comfyui/main.py")
OUTPUT_DIRS = [
    pathlib.Path("/runpod-volume/ComfyUI/output"),
    pathlib.Path("/comfyui/output"),
]

logger = logging.getLogger("handler")
logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _ok(data: Any = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": meta or {},
    }


def _fail(message: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    logger.error("FAIL: %s", message)
    return {
        "ok": False,
        "error": message,
        "meta": meta or {},
    }


def _comfy_get(path: str, **kwargs) -> requests.Response:
    url = f"{COMFY_BASE}{path}"
    logger.info("GET %s", url)
    resp = requests.get(url, timeout=kwargs.pop("timeout", 20), **kwargs)
    return resp


def _comfy_post(path: str, **kwargs) -> requests.Response:
    url = f"{COMFY_BASE}{path}"
    logger.info("POST %s", url)
    resp = requests.post(url, timeout=kwargs.pop("timeout", 20), **kwargs)
    return resp


def _ensure_comfy_ready(timeout: int = 120) -> None:
    """
    Try /history and /health-check. If ComfyUI is not up, try to start it
    once and then wait until it responds or we time out.
    """
    start = time.time()

    def is_ready() -> bool:
        try:
            resp = _comfy_get("/history")
            if resp.ok:
                return True
        except Exception:
            pass

        try:
            resp = _comfy_get("/health-check")
            if resp.ok:
                return True
        except Exception:
            pass

        return False

    if is_ready():
        logger.info("ComfyUI already ready.")
        return

    # Try to launch ComfyUI once.
    logger.warning("ComfyUI not ready, attempting to launch it...")
    try:
        subprocess.Popen(
            [COMFY_PY, COMFY_MAIN, "--listen", "0.0.0.0", "--port", "8188"],
            cwd="/comfyui",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        logger.info("Launched ComfyUI with %s %s ...", COMFY_PY, COMFY_MAIN)
    except Exception as e:
        logger.exception("Failed to launch ComfyUI: %s", e)
        raise

    # Poll until ready or timeout
    while time.time() - start < timeout:
        if is_ready():
            logger.info("ComfyUI became ready after launch.")
            return
        time.sleep(2)

    raise RuntimeError("ComfyUI did not become ready within timeout.")


def _list_outputs() -> List[Dict[str, Any]]:
    images = []
    for root in OUTPUT_DIRS:
        if not root.exists():
            continue
        for p in root.glob("*.png"):
            try:
                stat = p.stat()
                images.append(
                    {
                        "name": p.name,
                        "path": str(p),
                        "mtime": int(stat.st_mtime),
                        "size": stat.st_size,
                    }
                )
            except Exception as e:
                logger.warning("Failed to stat %s: %s", p, e)
    images.sort(key=lambda x: x["mtime"])
    return images


def _scan_outputs() -> List[Dict[str, Any]]:
    return _list_outputs()


def _await_new_outputs(before: Dict[str, int], timeout: int = 300, poll_interval: float = 3.0):
    """
    Wait for new or updated files compared to 'before' mapping of path->mtime.
    Returns (new_images, after_mapping).
    """
    start = time.time()
    while time.time() - start < timeout:
        after_all = _scan_outputs()
        after_map = {i["path"]: i["mtime"] for i in after_all}

        new_images = []
        for img in after_all:
            old_mtime = before.get(img["path"])
            if old_mtime is None or img["mtime"] > old_mtime:
                new_images.append(img)

        if new_images:
            return new_images, after_map

        time.sleep(poll_interval)

    return [], before


# ---------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------


def _handle_ping(body):
    return _ok({"message": "pong"})


def _handle_about(body):
    info = {
        "message": "ImagineWorlds × RunPod · Flux + IP Adapter Worker",
        "comfy_base": COMFY_BASE,
        "output_dirs": [str(p) for p in OUTPUT_DIRS],
    }
    try:
        resp = _comfy_get("/history")
        info["comfy_history_ok"] = resp.ok
    except Exception as e:
        info["comfy_history_error"] = str(e)
    return _ok(info)


def _handle_list_outputs(body):
    return _ok({"images": _list_outputs()})


def _handle_generate(body):
    payload = body.get("payload") or {}
    workflow = payload.get("workflow") or payload.get("prompt")
    if not workflow:
        return _fail("no workflow provided")

    # Ensure a unique prompt_id for every run so ComfyUI history/results don't clash
    try:
        workflow["prompt_id"] = f"fluxip_{uuid.uuid4()}"
    except Exception as e:
        logger.warning("Failed to set custom prompt_id: %s", e)

    _ensure_comfy_ready()

    # Clear any previous ComfyUI prompt state to avoid collisions between runs
    try:
        requests.post(f"{COMFY_BASE}/prompt", json={"clear": True}, timeout=5)
    except Exception as e:
        logger.warning("Failed to clear ComfyUI prompt state: %s", e)

    before = {i["path"]: i["mtime"] for i in _scan_outputs()}

    try:
        resp = _comfy_post("/prompt", json=workflow)
        resp.raise_for_status()
        prompt_response = resp.json()
    except Exception as e:
        return _fail(f"generate failed: {e}")

    new_images, _after = _await_new_outputs(before)
    latest = sorted(new_images, key=lambda x: x["mtime"])[-1] if new_images else None

    return _ok(
        {
            "prompt_response": prompt_response,
            "new_images": new_images,
            "latest_image": latest,
        }
    )


def _handle_preflight(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Light-weight check that:
    - ComfyUI is reachable
    - core endpoints work
    - output dirs are visible
    """
    try:
        _ensure_comfy_ready()
    except Exception as e:
        return _fail(f"preflight: ComfyUI not ready: {e}")

    try:
        hist = _comfy_get("/history")
        hist_ok = hist.ok
        hist_status = hist.status_code
    except Exception as e:
        hist_ok = False
        hist_status = str(e)

    outputs = _list_outputs()

    return _ok(
        {
            "comfy_history_ok": hist_ok,
            "comfy_history_status": hist_status,
            "output_sample": outputs[-3:],
        }
    )


# ---------------------------------------------------------------------
# RunPod handler
# ---------------------------------------------------------------------


def handler(event: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod entrypoint.
    """
    body = event.get("input") or {}
    action = body.get("action") or "ping"

    logger.info("handler.action=%s", action)

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

    return _fail(f"unknown action: {action}")


runpod.serverless.start({"handler": handler})
