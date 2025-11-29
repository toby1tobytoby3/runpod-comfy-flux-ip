import importlib.util, json, logging, os, pathlib, time, subprocess, requests, runpod
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

# ---------------------------------------------------------------------
# ComfyUI Process Management
# ---------------------------------------------------------------------
_comfy_process: Optional[subprocess.Popen] = None


def _start_comfy_if_needed():
    global _comfy_process
    if _comfy_process:
        rc = _comfy_process.poll()
        if rc is None:
            logger.debug("ComfyUI already running pid=%s", _comfy_process.pid)
            return
        logger.warning("ComfyUI process previously exited rc=%s, restarting", rc)

    # Extra diagnostics for Flux + patching
    os.environ["TORCH_SHOW_LOADED_KEYS"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["COMFYUI_VERBOSE_STARTUP"] = "1"

    cmd = [
        COMFY_PYTHON,
        "main.py",
        "--listen",
        "0.0.0.0",
        "--port",
        str(COMFY_PORT),
    ]

    log_path = pathlib.Path(COMFY_LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", buffering=1)

    logger.info("Launching ComfyUI with verbose logging...")
    _comfy_process = subprocess.Popen(
        cmd,
        cwd=COMFY_DIR,
        stdout=log_file,
        stderr=log_file,
        text=True,
    )
    logger.info(f"Started ComfyUI pid={_comfy_process.pid}, log={COMFY_LOG_PATH}")


def _wait_for_comfy_ready(timeout: float = 90.0):
    start = time.time()
    while True:
        try:
            resp = requests.get(f"{COMFY_BASE}/system_stats", timeout=5)
            if resp.ok:
                logger.info("ComfyUI ready.")
                return
        except Exception as e:
            logger.debug(f"Waiting for ComfyUI: {e}")
        if time.time() - start > timeout:
            raise RuntimeError("Timed out waiting for ComfyUI startup")
        time.sleep(1)


def _ensure_comfy_ready():
    _start_comfy_if_needed()
    _wait_for_comfy_ready()

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _ok(data):
    return {"ok": True, "data": data}


def _fail(msg, *, error=None):
    logger.error(msg)
    return {"ok": False, "error": error or msg}


def _comfy_post(path, *, json=None, timeout=None):
    return requests.post(
        f"{COMFY_BASE}{path}",
        json=json,
        timeout=timeout or COMFY_REQUEST_TIMEOUT,
    )


def _scan_outputs():
    imgs = []
    for d in OUTPUT_SCAN_DIRS:
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".png"):
                    p = pathlib.Path(root) / f
                    try:
                        s = p.stat()
                    except Exception:
                        continue
                    imgs.append(
                        {
                            "path": str(p),
                            "name": p.name,
                            "mtime": int(s.st_mtime),
                            "size": s.st_size,
                        }
                    )
    imgs.sort(key=lambda x: x["mtime"])
    return imgs


def _await_new_outputs(
    before,
    wait_seconds: float = COMFY_OUTPUT_WAIT_SECONDS,
    poll_interval: float = COMFY_OUTPUT_POLL_INTERVAL,
):
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        imgs = _scan_outputs()
        new = [
            i
            for i in imgs
            if i["path"] not in before or i["mtime"] > before[i["path"]]
        ]
        if new:
            return new, {i["path"]: i["mtime"] for i in imgs}
        time.sleep(poll_interval)
    return [], before


def _tail_file(path, max_bytes: int = 8192) -> str:
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return f"(no log at {path})"
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            return f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"(error reading log: {e})"

# ---------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------
def _handle_ping():
    return _ok({"message": "pong"})


def _handle_preflight():
    """
    - Ensures ComfyUI is running
    - Returns system_stats
    - Also reports whether flux_double_stream_patch appears to have applied.
    """
    _ensure_comfy_ready()
    try:
        stats = requests.get(f"{COMFY_BASE}/system_stats", timeout=10).json()
    except Exception as e:
        return _fail(f"preflight failed: {e}")

    # Check patch status from log
    tail = _tail_file(COMFY_LOG_PATH)
    patch_ok = "flux_double_stream_patch: DoubleStreamBlock.forward patched successfully" in tail
    patch_error = "flux_double_stream_patch: could not apply patch" in tail
    import_failed = "IMPORT FAILED" in tail and "flux_double_stream_patch.py" in tail

    patch_file = pathlib.Path("/comfyui/custom_nodes/flux_double_stream_patch.py")
    patch_spec = importlib.util.find_spec("flux_double_stream_patch")

    patch_info = {
        "patched": patch_ok,
        "import_failed": import_failed,
        "log_path": COMFY_LOG_PATH,
        "patch_file_exists": patch_file.exists(),
        "import_spec_found": patch_spec is not None,
    }

    if patch_spec is not None:
        patch_info.update(
            {
                "import_spec_origin": getattr(patch_spec, "origin", None),
                "import_spec_submodule_search_locations": list(
                    patch_spec.submodule_search_locations or []
                ),
            }
        )

    logger.info(
        "patch diagnostics: exists=%s import_spec=%s origin=%s",
        patch_info["patch_file_exists"],
        patch_info["import_spec_found"],
        patch_info.get("import_spec_origin"),
    )

    if patch_error:
        patch_info["warning"] = "flux_double_stream_patch reported an error during apply."
    if not patch_ok and not import_failed:
        patch_info.setdefault("note", "No explicit success line yet — patch may apply only on first model use.")

    return _ok(
        {
            "system_stats": stats,
            "flux_double_stream_patch": patch_info,
        }
    )


def _handle_dump_comfy_log(body):
    lines = int(body.get("lines", 400))
    tail = _tail_file(COMFY_LOG_PATH)
    tail_lines = tail.splitlines()
    if len(tail_lines) > lines:
        tail = "\n".join(tail_lines[-lines:])
    return _ok({"log_tail": tail, "path": COMFY_LOG_PATH})


def _handle_list_all_outputs():
    return _ok({"images": _scan_outputs()})


def _handle_generate(body):
    """
    Generate an image via ComfyUI using a provided workflow/prompt.

    Adds detailed timing so we can see where the time is actually spent:
      - comfy_ready: time spent ensuring ComfyUI is up
      - prompt_submit: time to POST the workflow to /prompt
      - wait_outputs: time spent polling the output directory
      - total: end-to-end time inside this handler
    """
    payload = body.get("payload") or {}
    # support both {"payload": {"workflow": {...}}} and {"payload": {"prompt": {...}}}
    workflow = payload.get("workflow") or payload.get("prompt")

    if not workflow:
        return {
            "ok": False,
            "error": "No workflow/prompt provided in payload.workflow or payload.prompt",
        }

    # ── 1. Time ComfyUI startup / readiness ──────────────────────────────────────
    t_start = time.time()
    _ensure_comfy_ready()
    t_ready = time.time()

    # ── 2. Snapshot outputs before we fire the workflow ──────────────────────────
    before_all = _scan_outputs()
    before = {img["path"]: img["mtime"] for img in before_all}
    latest_mtime = max(before.values()) if before else 0
    logger.info(
        "generate: outputs before=%d latest_mtime=%s",
        len(before),
        latest_mtime,
    )

    # ── 3. POST the workflow to ComfyUI and time the submit ──────────────────────
    t_before_prompt = time.time()
    try:
        resp = _comfy_post("/prompt", json=workflow)
        resp.raise_for_status()
        prompt_response = resp.json()
    except Exception as e:
        t_after_prompt = time.time()
        logger.exception("generate: failed to submit prompt to ComfyUI")
        return {
            "ok": False,
            "error": f"Failed to submit prompt to ComfyUI: {e}",
            "timings": {
                "comfy_ready": t_ready - t_start,
                "prompt_submit": t_after_prompt - t_before_prompt,
                "wait_outputs": 0.0,
                "total": t_after_prompt - t_start,
            },
        }
    t_after_prompt = time.time()

    # ── 4. Wait for new outputs and time the wait loop ───────────────────────────
    t_before_wait = time.time()
    new_images, after = _await_new_outputs(before)
    t_after_wait = time.time()

    # Log a single timing line for easy grepping in logs
    logger.info(
        "generate timing: comfy_ready=%.2fs prompt_submit=%.2fs wait_outputs=%.2fs total=%.2fs",
        t_ready - t_start,
        t_after_prompt - t_before_prompt,
        t_after_wait - t_before_wait,
        t_after_wait - t_start,
    )

    timings = {
        "comfy_ready": t_ready - t_start,
        "prompt_submit": t_after_prompt - t_before_prompt,
        "wait_outputs": t_after_wait - t_before_wait,
        "total": t_after_wait - t_start,
    }

    # ── 5. Interpret results ─────────────────────────────────────────────────────
    if not new_images:
        # No new files appeared – return handler+comfy log tails so we can see why
        comfy_log = _read_file_tail("/comfyui/user/comfyui.log") or _read_file_tail(COMFY_LOG_PATH)
        logger.warning("generate: no new images detected; comfy log tail attached")
        return {
            "ok": False,
            "error": "No new images detected after generation",
            "prompt_response": prompt_response,
            "comfy_log_tail": comfy_log,
            "timings": timings,
        }

    latest_image = max(new_images, key=lambda p: after.get(p, 0))
    logger.info(
        "generate: new_images=%d latest=%s",
        len(new_images),
        latest_image,
    )

    return {
        "ok": True,
        "image_paths": new_images,
        "latest_image": latest_image,
        "prompt_response": prompt_response,
        "timings": timings,
    }


def handler(event):
    inp = (event or {}).get("input") or {}
    action = inp.get("action")
    body = {"payload": inp.get("payload")}

    logger.info(f"handler action={action}")

    try:
        if action == "ping":
            return _handle_ping()
        if action == "preflight":
            return _handle_preflight()
        if action == "dump_comfy_log":
            return _handle_dump_comfy_log(body)
        if action == "list_all_outputs":
            return _handle_list_all_outputs()
        if action == "generate":
            return _handle_generate(body)
        return _fail(f"unknown action: {action}")
    except Exception as e:
        logger.exception("Unhandled exception in handler")
        return _fail(str(e))


runpod.serverless.start({"handler": handler})
