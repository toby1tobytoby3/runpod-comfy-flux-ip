FROM runpod/worker-comfyui:5.5.0-base
ENV DEBIAN_FRONTEND=noninteractive

# Tools
RUN apt-get update && apt-get install -y --no-install-recommends jq curl wget git && rm -rf /var/lib/apt/lists/*

# Flux custom node
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# Handler
WORKDIR /workspace
COPY handler.py /workspace/handler.py

# ✅ Keep ComfyUI in /comfyui but align inputs/outputs
ENV COMFY_DIR=/comfyui
ENV INPUT_DIR=/workspace/ComfyUI/input
ENV OUTPUT_DIR=/workspace/ComfyUI/output
RUN mkdir -p /workspace/ComfyUI/input /workspace/ComfyUI/output && \
    ln -sf /workspace/ComfyUI/input /comfyui/input && \
    ln -sf /workspace/ComfyUI/output /comfyui/output

# 🔧 Bridge IP-Adapter model into Comfy's models directory
# This makes /runpod-volume/models/ip_adapter/ip_adapter.safetensors
# visible to x-flux-comfyui as an "ipadapters" choice.
RUN mkdir -p /comfyui/models/ipadapters && \
    ln -sf /runpod-volume/models/ip_adapter/ip_adapter.safetensors \
           /comfyui/models/ipadapters/ip_adapter.safetensors || true

# Safe optional patch for attn_mask only (no CLIP edits)
RUN python - <<'PY'
import os, re
flux_path = "/comfyui/comfy/ldm/flux/layers.py"
if os.path.exists(flux_path):
    src = open(flux_path).read()
    if "attn_mask=None" not in src and "class DoubleStreamBlock" in src:
        src = re.sub(r"def forward\\(self,[^)]*\\):", lambda m: m.group(0)[:-2] + ", attn_mask=None):", src, 1)
        open(flux_path, "w").write(src)
PY

# Final setup
RUN pip install --no-cache-dir runpod requests
CMD ["python3", "/workspace/handler.py"]
