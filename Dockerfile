# Fast, GPU-friendly base with RunPod worker + ComfyUI baked in
FROM runpod/worker-comfyui:5.5.0-base

# ---- minimal OS deps (curl/jq/ffmpeg handy for debugging & image ops) ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl jq ffmpeg psmisc \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# ---- make sure /workspace/ComfyUI exists (some bases use /comfyui) ----
RUN mkdir -p /workspace \
 && if [ ! -d /workspace/ComfyUI ] && [ -d /comfyui ]; then ln -s /comfyui /workspace/ComfyUI; fi

ENV COMFYUI_ROOT=/workspace/ComfyUI
WORKDIR ${COMFYUI_ROOT}

# ---- Python quality-of-life ----
ENV PYTHONUNBUFFERED=1

# ---- fix common ComfyUI dependency warnings/errors you saw in logs ----
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
 && python3 -m pip install --no-cache-dir \
      alembic \
      av \
      scikit-image \
      torchaudio || true

# ---- install/refresh core ComfyUI requirements (covers sqlite + templates msgs) ----
RUN python3 -m pip install --no-cache-dir \
      "comfyui_frontend_package==1.28.7" \
      "comfyui-workflow-templates==0.2.2"

# ---- install FLUX custom nodes (both common providers to guarantee class availability) ----
RUN mkdir -p ${COMFYUI_ROOT}/custom_nodes

# XLabs (IP-Adapter + FLUX bits)
RUN git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git ${COMFYUI_ROOT}/custom_nodes/x_flux_comfyui || true \
 && if [ -f ${COMFYUI_ROOT}/custom_nodes/x_flux_comfyui/requirements.txt ]; then \
       python3 -m pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/x_flux_comfyui/requirements.txt; \
    fi \
 && python3 - <<'PY'
import os
p="/workspace/ComfyUI/custom_nodes/x_flux_comfyui/__init__.py"
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p,"a").close()
PY

# Fallback/companion FLUX node pack (ensures T5XXLLoader/FluxGuidance are present even if one repo changes)
# Using a widely used Flux node collection (name/path chosen to avoid clashes).
RUN git clone --depth 1 https://github.com/fofr/ComfyUI-Flux.git ${COMFYUI_ROOT}/custom_nodes/comfyui_flux || true \
 && if [ -f ${COMFYUI_ROOT}/custom_nodes/comfyui_flux/requirements.txt ]; then \
       python3 -m pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/comfyui_flux/requirements.txt; \
    fi \
 && python3 - <<'PY'
import os
p="/workspace/ComfyUI/custom_nodes/comfyui_flux/__init__.py"
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p,"a").close()
PY

# ---- ensure ComfyUI searches both common custom-node locations ----
ENV COMFYUI_CUSTOM_NODE_PATH=/workspace/ComfyUI/custom_nodes:/comfyui/custom_nodes
# add /workspace so Python can import /workspace/handler.py
ENV PYTHONPATH=/workspace:/workspace/ComfyUI/custom_nodes:/comfyui/custom_nodes:${PYTHONPATH}

# ---- point models to the RunPod network volume ----
# (this matches what we see in the logs: /runpod-volume/models/...)
ENV COMFYUI_MODEL_PATHS=/runpod-volume/models
ENV COMFYUI_MODELS_DIR=/runpod-volume/models

# Standard subdirs we use (not required, but helpful for clarity)
RUN mkdir -p /runpod-volume/models/{checkpoints,clip_vision,ip_adapter,loras,t5xxl,clip,vae}

# ---- copy your serverless handler & tell the worker where to find it ----
# Put handler as /handler.py and reference it as module "handler"
COPY handler.py /handler.py
ENV RUNPOD_HANDLER_MODULE=handler

# ---- Simplified healthcheck: just verify ComfyUI is up ----
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=10 CMD \
  curl -sf http://127.0.0.1:8188/object_info || exit 1


