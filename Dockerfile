# Public, small base image
FROM runpod/worker-comfyui:5.5.0-base

# Minimal tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# ComfyUI can live at /workspace/ComfyUI or /comfyui depending on base;
# make /workspace/ComfyUI always exist and point to /comfyui if needed.
RUN mkdir -p /workspace \
 && if [ ! -d /workspace/ComfyUI ] && [ -d /comfyui ]; then ln -s /comfyui /workspace/ComfyUI; fi

ENV COMFYUI_ROOT=/workspace/ComfyUI
WORKDIR ${COMFYUI_ROOT}

# Install XLabs FLUX custom nodes where ComfyUI will load them
RUN mkdir -p ${COMFYUI_ROOT}/custom_nodes \
 && git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui \
 && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui/requirements.txt

# Make sure both customary locations are searched for custom nodes
ENV COMFYUI_CUSTOM_NODE_PATH=/workspace/ComfyUI/custom_nodes:/comfyui/custom_nodes

# Point model search to your RunPod volume
ENV COMFYUI_MODEL_PATHS=/runpod-volume/models
ENV COMFYUI_MODELS_DIR=/runpod-volume/models

# Serverless handler shim (re-export)
COPY handler.py /workspace/handler.py
ENV RUNPOD_HANDLER_MODULE=/workspace/handler
