# lean base: models come from your mounted network volume
FROM runpod/worker-comfyui:5.5.0-base

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# IMPORTANT: this is where the worker's ComfyUI lives
ENV COMFYUI_ROOT=/workspace/ComfyUI
WORKDIR ${COMFYUI_ROOT}

# Install XLabs nodes right where ComfyUI will load them
RUN mkdir -p ${COMFYUI_ROOT}/custom_nodes \
 && git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui \
 && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui/requirements.txt

# Optional: ControlNet preprocessors (commented)
# RUN git clone --depth 1 https://github.com/Fannovel16/comfyui_controlnet_aux.git ${COMFYUI_ROOT}/custom_nodes/comfyui_controlnet_aux \
#  && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/comfyui_controlnet_aux/requirements.txt

# Point ComfyUI to your model volume
ENV COMFYUI_MODEL_PATHS=/runpod-volume/models
ENV COMFYUI_MODELS_DIR=/runpod-volume/models
ENV WORKFLOW_DIR=${COMFYUI_ROOT}/workflows

# Serverless handler shim (re-export base image handler)
COPY handler.py /workspace/handler.py
ENV RUNPOD_HANDLER_MODULE=/workspace/handler
