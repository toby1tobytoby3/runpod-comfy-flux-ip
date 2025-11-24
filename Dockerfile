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

# Safe optional patch for attn_mask only (no CLIP edits) – unchanged
RUN python - <<'PY'
import os, re
flux_path = "/comfyui/comfy/ldm/flux/layers.py"
if os.path.exists(flux_path):
    src = open(flux_path).read()
    if "attn_mask=None" not in src and "class DoubleStreamBlock" in src:
        src = re.sub(r"def forward\(self,[^)]*\):", lambda m: m.group(0)[:-2] + ", attn_mask=None):", src, 1)
        open(flux_path, "w").write(src)
PY

# Final setup – unchanged
RUN pip install --no-cache-dir runpod requests
CMD ["python3", "/workspace/handler.py"]
