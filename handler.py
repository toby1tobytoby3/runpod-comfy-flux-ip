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
COMFY = f"http://{COMFY_HOST}:{COMFY_PORT}"

INPUT_DIR = "/workspace/ComfyUI/input"
OUTPUT_DIR = "/workspace/ComfyUI/output"

# Minimal set of nodes the FLUX graph expects on your worker
REQUIRED_NODES = {
    "CheckpointLoaderSimple",
    "EmptyLatentImage",
    "CLIPTextEncodeFlux",
    "KSampler",
    "VAEDecode",
    "SaveImage",
    "CLIPLoader",
    "FluxGuidance",
    "DualCLIPLoader",
    "ModelSamplingFlux",  # optional but nice to assert
}

DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<b64>.+)$")


def _ok(data: Any = None, **extra):
    out = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(extra)
    return out


def _err(message: str, *, type_: str = "error", **extra):
    return {"error": {"type": type_, "message": message, "extra_info": extra}}


def _json(x: requests.Response) -> Dict[str, Any]:
    try:
        return x.json()
    except Exception:
        return {"_raw": x.text, "_status": x.status_code}


def _ensure_dirs(path: str):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def _save_data_uri(filename: str, data_uri: str, subdir: str | None = None) -> str:
    m = DATA_URI_RE.match(data_uri)
    if not m:
        raise ValueError("Not a base64 data URI")
    b64 = m.group("b64")
    data = base64.b64decode(b64)

    target_dir = INPUT_DIR if not subdir else os.path.join(INPUT_DIR, subdir)
    _ensure_dirs(target_dir)
    # make filename safe
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", filename)
    # prefix with timestamp to avoid collisions
    ts = int(time.time() * 1000)
    fn = f"{ts}_{safe}"
    full = os.path.join(target_dir, fn)
    with open(full, "wb") as f:
        f.write(data)
    return fn if not subdir else f"{subdir}/{fn}"


def _object_info() -> Dict[str, Any]:
    r = requests.get(f"{COMFY}/object_info", timeout=10)
    return _json(r)


def _features_from_object_info(obj: Dict[str, Any]) -> Dict[str, bool]:
    keys = set(obj.keys()) if isinstance(obj, dict) else set()
    return {
        "ip_adapter": all(k in keys for k in [
            "LoadFluxIPAdapter", "ApplyFluxIPAdapter"
        ]),
        "lora": "LoraLoader" in keys or "LoraLoaderModelOnly" in keys,
        "flux_core": all(k in keys for k in [
            "T5XXLLoader", "CLIPLoader", "FluxGuidance", "CLIPTextEncodeFlux"
        ]),
    }


def _validate_minimal_graph(graph: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(graph, dict) or "prompt" not in graph:
        return (False, "Graph must be an object with a 'prompt' field.")
    prompt = graph["prompt"]
    if not isinstance(prompt, dict) or not prompt:
        return (False, "'prompt' must be a non-empty object.")
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            return (False, f"Node {node_id} must be an object.")
        if "class_type" not in node:
            return (False, f"Node {node_id} missing 'class_type'.")
        if "inputs" not in node:
            return (False, f"Node {node_id} missing 'inputs'.")
    return (True, "ok")


def _post_prompt(graph: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(
        f"{COMFY}/prompt",
        json=graph,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    return _json(r)


def _poll_history(prompt_id: str, timeout_s: int = 180) -> Dict[str, Any]:
    t0 = time.time()
    while True:
        r = requests.get(f"{COMFY}/history/{prompt_id}", timeout=10)
        j = _json(r)
        # Comfy returns a dict keyed by prompt_id
        if isinstance(j, dict) and prompt_id in j:
            hist = j[prompt_id]
            # consider done when "outputs" exists (success)
            if "outputs" in hist:
                return {"status": "completed", "history": hist}
        if time.time() - t0 > timeout_s:
            return _err("Timeout polling history", type_="timeout", last=j)
        time.sleep(1.2)


def _preflight_nodes() -> Dict[str, Any]:
    oi = _object_info()
    if not isinstance(oi, dict) or not oi:
        return _err(
            "ComfyUI /object_info unavailable",
            type_="comfy_unreachable",
            url=f"{COMFY}/object_info",
            response=oi,
        )

    available = set(oi.keys())
    missing = sorted(list(REQUIRED_NODES - available))
    return _ok({
        "available_count": len(available),
        "missing": missing,
        "all_good": len(missing) == 0,
    }) if not missing else _err(
        "Required ComfyUI nodes are missing on the worker.",
        type_="missing_nodes",
        missing=missing,
        hint="Ensure FLUX custom nodes are installed and imported (see Dockerfile changes).",
        comfy_object_info_sample=list(
            sorted(k for k in available if k.startswith("T") or k.startswith("C"))
        )[:40],
    )


def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runpod serverless entrypoint.
    expects event["input"] with:
      - action: "ping"|"about"|"features"|"preflight"|"debug_ip_paths"|"upload"|"generate"
    """
    try:
        inp = (event or {}).get("input") or {}
        action = (inp.get("action") or "").lower().strip()

        # 1) Health ping
        if action == "ping":
            return _ok({"status": "alive"})

        # 2) About: minimal Comfy details
        if action == "about":
            oi = _object_info()
            return _ok({
                "comfy": {"host": COMFY_HOST, "port": COMFY_PORT},
                "has_object_info": isinstance(oi, dict) and bool(oi),
            })

        # 3) Features: discover IP-Adapter + LoRA + FLUX core support
        if action == "features":
            oi = _object_info()
            return _ok({"supports": _features_from_object_info(oi)})

        # 4) Preflight: explicitly verify required nodes exist (fast failure)
        if action == "preflight":
            return _preflight_nodes()

        # 4b) Debug: check IP-Adapter-related paths on the actual worker
        if action == "debug_ip_paths":
            paths = [
                "/workspace/models/checkpoints/flux1-dev.safetensors",
                "/workspace/models/ipadapter/ip_adapter.safetensors",
                "/workspace/models/xlabs/ipadapters/ip_adapter.safetensors",
                "/workspace/models/clip_vision/sigclip_vision_patch14_384.safetensors",
                "/workspace/ComfyUI/models/checkpoints/flux1-dev.safetensors",
                "/workspace/ComfyUI/models/ipadapter/ip_adapter.safetensors",
                "/workspace/ComfyUI/models/xlabs/ipadapters/ip_adapter.safetensors",
                "/workspace/ComfyUI/models/clip_vision/sigclip_vision_patch14_384.safetensors",
                "/workspace/ComfyUI/input/ip_ref.png",
            ]
            exists = {p: os.path.exists(p) for p in paths}
            return _ok({"paths": exists})

        # 5) Upload: save one or many images into /input[/subdir]
        if action == "upload":
            files = []
            # single-file convenience
            if "filename" in inp and "dataUri" in inp:
                subdir = inp.get("subdir")
                saved = _save_data_uri(inp["filename"], inp["dataUri"], subdir)
                files.append(saved)
            # multi-file
            elif isinstance(inp.get("files"), list):
                subdir = inp.get("subdir")
                for f in inp["files"]:
                    saved = _save_data_uri(f["filename"], f["dataUri"], subdir)
                    files.append(saved)
            else:
                return _err(
                    "Provide (filename,dataUri) or files[].",
                    type_="upload_bad_request",
                )

            return _ok({"saved": files})

        # 6) Generate: validate-only or full run
        if action == "generate":
            wf = inp.get("workflow")
            if not wf:
                return _err(
                    "Missing 'workflow' parameter", type_="generate_bad_request"
                )

            validate_only = bool(inp.get("validate_only"))
            valid, why = _validate_minimal_graph(
                wf if "prompt" in wf else {"prompt": wf}
            )
            if not valid:
                return _err(
                    f"Invalid workflow: {why}", type_="invalid_prompt"
                )

            # Preflight the required nodes; bubble up precise missing list
            pf = _preflight_nodes()
            if "error" in pf:
                return pf  # missing nodes or comfy unreachable

            if validate_only:
                return _ok({"validated": True, "note": why})

            # Ensure shape for Comfy: top-level keys "client_id?" and "prompt"
            if "prompt" not in wf:
                wf = {"prompt": wf}

            # If client_id not provided, add one (helps Comfy routing)
            wf.setdefault("client_id", f"rp_{int(time.time() * 1000)}")

            sub = _post_prompt(wf)
            if "prompt_id" not in sub:
                # Bubble up Comfy errors
                return _err(
                    "Submission failed",
                    type_="comfy_submit_failed",
                    response=sub,
                )

            pid = sub["prompt_id"]
            hist = _poll_history(pid)
            return _ok({"prompt_id": pid, "result": hist})

        # Default: unknown action
        return _err(f"Unknown action '{action}'", type_="unknown_action")

    except Exception as e:
        return _err(str(e), type_="exception")
