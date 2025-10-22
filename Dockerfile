FROM runpod/worker-comfyui:5.5.0-flux1-dev

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

ENV COMFYUI_ROOT=/comfyui
WORKDIR ${COMFYUI_ROOT}

# XLabs IP-Adapter nodes
RUN mkdir -p ${COMFYUI_ROOT}/custom_nodes \
 && git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui \
 && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/x-flux-comfyui/requirements.txt

# (Optional) ControlNet preprocessors
# RUN git clone --depth 1 https://github.com/Fannovel16/comfyui_controlnet_aux.git ${COMFYUI_ROOT}/custom_nodes/comfyui_controlnet_aux \
#  && pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/comfyui_controlnet_aux/requirements.txt

# Tell ComfyUI where your models live at runtime (RunPod network volume)
ENV COMFYUI_MODEL_PATHS=/runpod-volume/models
ENV COMFYUI_MODELS_DIR=/runpod-volume/models
ENV WORKFLOW_DIR=${COMFYUI_ROOT}/workflows

# Hub/Serverless “handler required” shim (re-export base image handler)
COPY handler.py /workspace/handler.py
ENV RUNPOD_HANDLER_MODULE=/workspace/handler
