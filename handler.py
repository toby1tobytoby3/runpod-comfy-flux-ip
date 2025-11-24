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

# Base defaults from image
COMFY_DIR = os.getenv("COMFY_DIR", "/workspace/ComfyUI")
COMFY_PYTHON = os.getenv("COMFY_PYTHON", "python3")
COMFY_LOG_PATH = os.getenv("COMFY_LOG_PATH", "/tmp/comfy.log")

# ✅ Updated to match ComfyUI’s true runtime paths
COMFY_DIR = os.getenv("COMFY_DIR", "/comfyui")  # revert to base image default
INPUT_DIR = os.getenv("INPUT_DIR", "/workspace/ComfyUI/input")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/workspace/ComfyUI/output")

SERVICE_NAME = "runpod-comfy-flux-ip"
SERVICE_VERSION = "v11"

# Extra candidate output dirs we might want to scan
OUTPUT_SCAN_DIRS: List[str] = list(dict.fromkeys([
    OUTPUT_DIR,
    "/comfyui/output",
    "/comfyui/user",
    "/comfyui/user/output",
    "/workspace/ComfyUI/output",
]))

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
logger.info("OUTPUT_SCAN_DIRS=%s", OUTPUT_SCAN_DIRS)

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
    """Internal helper: GET JSON from Comfy."""
    _ensure_comfy_ready()
    if not path.startswith("/"):
        path = "/" + path
    url = COMFY_BASE + path
    logger.info("GET %s", url)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _comfy_post_json(path: str, payload: Dict[str, Any], timeout: float = 60.0):
    """Internal helper: POST JSON to Comfy."""
    _ensure_comfy_ready()
    if not path.startswith("/"):
        path = "/" + path
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


def _scan_outputs_multi() -> Dict[str, Dict[str, Any]]:
    """
    Scan all OUTPUT_SCAN_DIRS for image files and return a mapping:
      full_path -> {name, path, size, mtime}
    """
    results: Dict[str, Dict[str, Any]] = {}
    for root_dir in OUTPUT_SCAN_DIRS:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        for root, _, names in os.walk(root_dir):
            for n in names:
                lower = n.lower()
                if not lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    continue
                path = os.path.join(root, n)
                try:
                    st = os.stat(path)
                    size = st.st_size
                    mtime = st.st_mtime
                except OSError:
                    size = None
                    mtime = None
                results[path] = {
                    "name": n,
                    "path": path,
                    "size": size,
                    "mtime": mtime,
                }
    return results


def _diff_outputs(before: Dict[str, Dict[str, Any]],
                  after: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Given two scan results, return a list of new/changed images:
    - New paths
    - Same path but changed mtime or size
    """
    changed: List[Dict[str, Any]] = []
    for path, meta in after.items():
        prev = before.get(path)
        if prev is None:
            changed.append(meta)
        else:
            if meta.get("size") != prev.get("size") or meta.get("mtime") != prev.get("mtime"):
                changed.append(meta)
    # Sort newest first by mtime
    changed.sort(key=lambda x: (x.get("mtime") or 0), reverse=True)
    return changed


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
            "OUTPUT_DIR": OUTPUT_DIR,
            "OUTPUT_SCAN_DIRS": OUTPUT_SCAN_DIRS,
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
    """
    Generate with Comfy and actively watch for new/changed images.

    Behaviour:
      1. Snapshot all known output dirs (OUTPUT_SCAN_DIRS) before the run.
      2. POST workflow to /prompt.
      3. Poll for up to WAIT_SECONDS looking for new/changed images.
      4. Return prompt_response + any detected new_images + wait_info.
    """
    # Ensure output dir is writable
    try:
        if OUTPUT_DIR:
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

    # Snapshot outputs before
    before = _scan_outputs_multi()
    logger.info("Pre-generate outputs: %d files", len(before))

    try:
        resp = _comfy_post_json("/prompt", wf, timeout=120.0)
    except Exception as e:
        return _fail(f"generate failed: {e}")

    prompt_id = None
    if isinstance(resp, dict):
        prompt_id = resp.get("prompt_id")

    # Poll for new/changed images
    WAIT_SECONDS = int(os.getenv("GENERATE_WAIT_SECONDS", "40"))
    POLL_INTERVAL = int(os.getenv("GENERATE_POLL_INTERVAL", "4"))
    deadline = time.time() + WAIT_SECONDS

    last_seen: Dict[str, Dict[str, Any]] = {}
    changed: List[Dict[str, Any]] = []

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        after = _scan_outputs_multi()
        changed = _diff_outputs(before, after)
        last_seen = after
        if changed:
            logger.info("Detected %d new/changed image(s) after generate", len(changed))
            break

    if not changed:
        logger.warning(
            "No new images detected after generate (waited %ss). before=%d after=%d",
            WAIT_SECONDS, len(before), len(last_seen),
        )

    return _ok({
        "prompt_response": resp,
        "prompt_id": prompt_id,
        "new_images": changed,
        "wait_info": {
            "wait_seconds": WAIT_SECONDS,
            "poll_interval": POLL_INTERVAL,
            "before_count": len(before),
            "after_count": len(last_seen),
            "output_scan_dirs": OUTPUT_SCAN_DIRS,
        },
    })


# ---- Helpers for debugging / inspection ----

def _handle_comfy_get(inp: Dict[str, Any]):
    """
    Generic GET pass-through to Comfy, for debugging.
    input.path: e.g. "/history/<prompt_id>" or "/object_info"
    """
    path = inp.get("path")
    if not path:
        return _fail("comfy_get requires 'path'")
    try:
        data = _comfy_get_json(path)
        return _ok({"path": path, "data": data})
    except Exception as e:
        return _fail(f"comfy_get failed: {e}")


def _handle_history(inp: Dict[str, Any]):
    """
    Convenience wrapper around /history/<prompt_id>.
    Expects:
      input.payload.prompt_id OR input.prompt_id
    """
    payload = inp.get("payload") or {}
    prompt_id = payload.get("prompt_id") or inp.get("prompt_id")
    if not prompt_id:
        return _fail("history requires payload.prompt_id or prompt_id")
    try:
        data = _comfy_get_json(f"/history/{prompt_id}")
        return _ok({"prompt_id": prompt_id, "history": data})
    except Exception as e:
        return _fail(f"history failed: {e}")


def _handle_list_outputs(_: Dict[str, Any]):
    """
    List image files under OUTPUT_DIR so we can confirm what's been saved.
    """
    root_dir = OUTPUT_DIR or "/comfyui/output"
    images: List[Dict[str, Any]] = []
    if not os.path.isdir(root_dir):
        return _ok({"images": [], "note": f"{root_dir} does not exist"})
    for root, _, names in os.walk(root_dir):
        for n in names:
            lower = n.lower()
            if lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                path = os.path.join(root, n)
                try:
                    size = os.path.getsize(path)
                    mtime = os.path.getmtime(path)
                except OSError:
                    size = None
                    mtime = None
                images.append({
                    "name": n,
                    "path": path,
                    "size": size,
                    "mtime": mtime,
                })
    # Sort newest first for convenience
    images.sort(key=lambda x: (x["mtime"] or 0), reverse=True)
    return _ok({"images": images})


def _handle_list_all_outputs(_: Dict[str, Any]):
    """
    More exhaustive listing across OUTPUT_SCAN_DIRS, to see if Comfy is
    writing anywhere unexpected.
    """
    scans = _scan_outputs_multi()
    images = list(scans.values())
    images.sort(key=lambda x: (x.get("mtime") or 0), reverse=True)
    return _ok({"images": images, "scan_dirs": OUTPUT_SCAN_DIRS})


def _handle_debug_ip_paths(_: Dict[str, Any]):
    """
    Inspect LoadFluxIPAdapter input fields from /object_info.
    Very useful to confirm ipadatper / clip_vision options.
    """
    try:
        info = _comfy_get_json("/object_info")
        lf = info.get("LoadFluxIPAdapter") or {}
        # Extract just the key bits
        inp = (lf.get("input") or {}).get("required") or {}
        return _ok({
            "LoadFluxIPAdapter": {
                "ipadatper_field": inp.get("ipadatper"),
                "clip_vision_field": inp.get("clip_vision"),
            }
        })
    except Exception as e:
        return _fail(f"debug_ip_paths failed: {e}")


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
        if action == "comfy_get":
            return _handle_comfy_get(inp)
        if action == "history":
            return _handle_history(inp)
        if action == "list_outputs":
            return _handle_list_outputs(inp)
        if action == "list_all_outputs":
            return _handle_list_all_outputs(inp)
        if action == "debug_ip_paths":
            return _handle_debug_ip_paths(inp)
        return _fail(f"unknown action '{action}'")
    except Exception as e:
        return _fail(f"unhandled exception: {e}")


runpod.serverless.start({"handler": handler})
