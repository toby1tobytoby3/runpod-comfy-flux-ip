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

# ---- fix common ComfyUI dependency warnings/errors you saw in logs ----
# - alembic needed for local sqlite db
# - pyav upgrade message from canary check
# - scikit-image used by controlnet_aux (silences those import errors)
# - torchaudio optional; prevents "torchaudio missing" warnings
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
 && python3 -m pip install --no-cache-dir \
      alembic \
      av \
      scikit-image \
      torchaudio || true

# ---- install/refresh core ComfyUI requirements (covers sqlite + templates msgs) ----
# These two packages removed the red-banner errors in your logs:
RUN python3 -m pip install --no-cache-dir \
      "comfyui_frontend_package==1.28.7" \
      "comfyui-workflow-templates==0.2.2"

# ---- install XLabs FLUX nodes (IP-Adapter) ----
# We keep it under a stable import-friendly path and ensure __init__.py exists.
RUN mkdir -p ${COMFYUI_ROOT}/custom_nodes \
 && git clone --depth 1 https://github.com/XLabs-AI/x-flux-comfyui.git ${COMFYUI_ROOT}/custom_nodes/x_flux_comfyui \
 && python3 - <<'PY'
import os
p="/workspace/ComfyUI/custom_nodes/x_flux_comfyui/__init__.py"
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p,"a").close()
PY
# (Optional) If the repo ships its own requirements, install them:
RUN if [ -f ${COMFYUI_ROOT}/custom_nodes/x_flux_comfyui/requirements.txt ]; then \
       python3 -m pip install --no-cache-dir -r ${COMFYUI_ROOT}/custom_nodes/x_flux_comfyui/requirements.txt; \
    fi

# ---- ensure ComfyUI searches both common custom-node locations ----
ENV COMFYUI_CUSTOM_NODE_PATH=/workspace/ComfyUI/custom_nodes:/comfyui/custom_nodes

# ---- point models to the RunPod network volume ----
# (this keeps checkpoints, CLIP-ViT, and IP-Adapter weights persistent)
ENV COMFYUI_MODEL_PATHS=/runpod-volume/models
ENV COMFYUI_MODELS_DIR=/runpod-volume/models

# Standard subdirs we use (not required, but helpful for clarity)
RUN mkdir -p /runpod-volume/models/{checkpoints,clip_vision,ip_adapter,loras}

# ---- copy your serverless handler & shim and tell the worker where to find it ----
# (keep filenames the same as in your repo)
COPY handler.py /workspace/handler.py
COPY handler_shim.py /workspace/handler_shim.py

# The worker entrypoint uses this to import your handler module
# If your actual handler callable is in handler.py as "handler", this is correct:
ENV RUNPOD_HANDLER_MODULE=/workspace/handler

# Note: we keep the base image ENTRYPOINT/CMD as-is (RunPod Worker),
# so your serverless endpoint behaves exactly like before.
