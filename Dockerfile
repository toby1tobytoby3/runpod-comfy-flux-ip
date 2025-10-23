# Keep the same base you’ve been using (flux1-dev)
FROM runpod/ai-api-comfy:5.5.0-flux1-dev

# Minimal tools
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# === IMPORTANT: install FLUX custom nodes where ComfyUI actually looks ===
# ComfyUI Base/Path is /comfyui (see logs), so custom nodes must live in /comfyui/custom_nodes/*
RUN git clone --depth=1 https://github.com/XLabs-AI/x-flux-comfyui /comfyui/custom_nodes/x-flux-comfyui \
 && pip install --no-cache-dir -r /comfyui/custom_nodes/x-flux-comfyui/requirements.txt || true

# Optional: prove at build-time that the node files exist (helps debugging)
RUN test -d /comfyui/custom_nodes/x-flux-comfyui && find /comfyui/custom_nodes -maxdepth 2 -type d -print

# Expose extra model paths (the base image already adds /runpod-volume/*, we keep that behavior)
ENV COMFYUI_CUSTOM_NODE_PATH=/comfyui/custom_nodes
