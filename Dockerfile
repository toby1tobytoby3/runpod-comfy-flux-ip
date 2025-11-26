FROM runpod/worker-comfyui:5.5.0-base
ENV DEBIAN_FRONTEND=noninteractive

# Core tools
RUN apt-get update && apt-get install -y --no-install-recommends jq curl wget git && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------
# Flux custom node (XLabs)
# ---------------------------------------------------------------------
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# ---------------------------------------------------------------------
# Copy handler
# ---------------------------------------------------------------------
WORKDIR /workspace
COPY handler.py /workspace/handler.py

# ---------------------------------------------------------------------
# Shared I/O and model paths
# ---------------------------------------------------------------------
ENV COMFY_DIR=/comfyui
ENV INPUT_DIR=/runpod-volume/ComfyUI/input
ENV OUTPUT_DIR=/runpod-volume/ComfyUI/output

RUN mkdir -p /runpod-volume/ComfyUI/input /runpod-volume/ComfyUI/output && \
    rm -rf /comfyui/input /comfyui/output && \
    ln -sf /runpod-volume/ComfyUI/input /comfyui/input && \
    ln -sf /runpod-volume/ComfyUI/output /comfyui/output

# Expose IP-Adapter model (both standard and XLabs layouts)
RUN mkdir -p /comfyui/models/ipadapters && \
    ln -sf /runpod-volume/models/ip_adapter/ip_adapter.safetensors /comfyui/models/ipadapters/ip_adapter.safetensors || true && \
    mkdir -p /comfyui/models/xlabs && \
    ln -sf /runpod-volume/models/xlabs/ipadapters /comfyui/models/xlabs/ipadapters || true

# ---------------------------------------------------------------------
# Flux DoubleStreamBlock patch — now as a valid custom node
# ---------------------------------------------------------------------
RUN python - <<'PY'
import pathlib, textwrap
p = pathlib.Path("/comfyui/custom_nodes/flux_double_stream_patch.py")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(textwrap.dedent("""
    import logging

    log = logging.getLogger(__name__)

    try:
        from comfy.ldm.flux.model import DoubleStreamBlock
        _orig_forward = DoubleStreamBlock.forward

        def _patched_forward(self, *args, **kwargs):
            kwargs.pop("attn_mask", None)
            return _orig_forward(self, *args, **kwargs)

        DoubleStreamBlock.forward = _patched_forward
        log.info("✅ flux_double_stream_patch: DoubleStreamBlock.forward patched successfully")
    except Exception as e:
        log.warning("⚠️ flux_double_stream_patch: could not apply patch: %s", e)

    # Dummy exports so ComfyUI registers this file
    NODE_CLASS_MAPPINGS = {}
    NODES_LIST = []
"""))
print("Wrote /comfyui/custom_nodes/flux_double_stream_patch.py")
PY

# ---------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------
RUN pip install --no-cache-dir runpod requests

# ---------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------
CMD ["python3", "/workspace/handler.py"]
