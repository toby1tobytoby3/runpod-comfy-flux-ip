# 1. Base: official ComfyUI worker, no models baked in
FROM runpod/worker-comfyui:5.5.0-base

ENV DEBIAN_FRONTEND=noninteractive

# 2. Small quality-of-life tools (optional, but handy)
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq curl wget git \
    && rm -rf /var/lib/apt/lists/*

# 3. Install XLabs / Flux custom nodes the supported way
#    (These live on the image; models live on the /workspace volume.)
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# 4. Copy our custom RunPod handler into the image
#    We put it in /workspace and ensure /workspace is on PYTHONPATH,
#    so "handler:handler" can be imported.
WORKDIR /workspace
COPY handler.py /workspace/handler.py

ENV PYTHONPATH="/workspace:${PYTHONPATH}"
ENV RUNPOD_HANDLER_MODULE=handler:handler

# 5. Do NOT override the entrypoint or healthcheck.
#    The base image already:
#      - runs ComfyUI
#      - exposes the serverless handler loop
#      - knows how to talk to RunPod's queue system.
#
#    Models will be picked up from /workspace/models when your
#    network volume is mounted there.
