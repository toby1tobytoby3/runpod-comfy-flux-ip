# 1. Base: official ComfyUI worker, no models baked in
FROM runpod/worker-comfyui:5.5.0-base

ENV DEBIAN_FRONTEND=noninteractive

# 2. Small quality-of-life tools (optional, but handy)
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq curl wget git \
    && rm -rf /var/lib/apt/lists/*

# 3. (Optional) Install XLabs / Flux custom nodes the *supported* way
#    If you already have them on the volume and don't need more, you can skip this.
#    This uses the helper that the base image ships with.
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# 4. DO NOT override the handler module, entrypoint, or healthcheck.
#    The base image already:
#      - runs ComfyUI
#      - exposes the serverless handler
#      - knows how to talk to RunPod's queue system.
#
#    So we DON'T set:
#      - RUNPOD_HANDLER_MODULE
#      - custom HEALTHCHECK
#      - COMFYUI_MODEL_PATHS
#
#    Models will be picked up automatically from /workspace/models when your
#    network volume is mounted there.
