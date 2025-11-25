FROM runpod/worker-comfyui:5.5.0-base
ENV DEBIAN_FRONTEND=noninteractive

# Tools
RUN apt-get update && apt-get install -y --no-install-recommends jq curl wget git && rm -rf /var/lib/apt/lists/*

# Flux custom node
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# Handler
WORKDIR /workspace
COPY handler.py /workspace/handler.py

# ✅ Keep ComfyUI in /comfyui
ENV COMFY_DIR=/comfyui

# ✅ Make handler + Comfy share the SAME IO dirs on the network volume
ENV INPUT_DIR=/runpod-volume/ComfyUI/input
ENV OUTPUT_DIR=/runpod-volume/ComfyUI/output

RUN mkdir -p /runpod-volume/ComfyUI/input /runpod-volume/ComfyUI/output && \
    rm -rf /comfyui/input /comfyui/output && \
    ln -sf /runpod-volume/ComfyUI/input /comfyui/input && \
    ln -sf /runpod-volume/ComfyUI/output /comfyui/output

# ✅ Expose IP-Adapter model to Comfy (for LoadFluxIPAdapter)
RUN mkdir -p /comfyui/models/ipadapters && \
    ln -sf /runpod-volume/models/ip_adapter/ip_adapter.safetensors \
           /comfyui/models/ipadapters/ip_adapter.safetensors || true

# (Optional: also mirror any previous xlabs/ipadapters layout if it exists)
RUN mkdir -p /comfyui/models/xlabs && \
    ln -sf /runpod-volume/models/xlabs/ipadapters \
           /comfyui/models/xlabs/ipadapters || true

# Flux attn_mask patch – custom node that strips legacy attn_mask kwarg
RUN python - <<'PY'
import pathlib, textwrap

patch_path = pathlib.Path("/comfyui/custom_nodes/flux_double_stream_patch.py")
patch_path.parent.mkdir(parents=True, exist_ok=True)
patch_path.write_text(textwrap.dedent("""
    import logging

    log = logging.getLogger(__name__)

    try:
        from comfy.ldm.flux.model import DoubleStreamBlock
    except Exception as e:
        log.warning("flux_double_stream_patch: could not import DoubleStreamBlock: %s", e)
    else:
        _orig_forward = DoubleStreamBlock.forward

        def _patched_forward(self, *args, **kwargs):
            # Drop legacy/extra attention mask kwarg if present
            kwargs.pop("attn_mask", None)
            return _orig_forward(self, *args, **kwargs)

        DoubleStreamBlock.forward = _patched_forward
        log.info("flux_double_stream_patch: DoubleStreamBlock.forward patched to ignore attn_mask kwarg")
"""))
print("Wrote flux_double_stream_patch custom node to", patch_path)
PY

# Final setup – unchanged
RUN pip install --no-cache-dir runpod requests
CMD ["python3", "/workspace/handler.py"]
