# Use the SMALL base (no baked models). We’ll mount models from the volume at runtime.
FROM runpod/worker-comfyui:5.5.0-base

# Minimal tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# ComfyUI root provided by the base image
ENV COMFYUI_ROOT=/comfyui
WORKDIR ${COMFYUI_ROOT}

# ---- XLabs IP-Adapter nodes (small; safe to bake) ----
RUN mkdir -p ${COMFYUI_ROOT}/custom_nodes \
 && git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui \
 && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui/requirements.txt

# (Optional) ControlNet preprocessors later if you need them:
# RUN git clone --depth 1 https://github.com/Fannovel16/comfyui_controlnet_aux.git ${COMFYUI_ROOT}/custom_nodes/comfyui_controlnet_aux \
#  && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/comfyui_controlnet_aux/requirements.txt

# Tell ComfyUI to look for models on the attached RunPod volume
ENV COMFYUI_MODEL_PATHS=/runpod-volume/models
ENV COMFYUI_MODELS_DIR=/runpod-volume/models
ENV WORKFLOW_DIR=${COMFYUI_ROOT}/workflows

# RunPod serverless requires a handler symbol in *this* repo; re-export base handler.
COPY handler.py /workspace/handler.py
ENV RUNPOD_HANDLER_MODULE=/workspace/handler
