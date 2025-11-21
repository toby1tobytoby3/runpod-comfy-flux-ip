# 1. Base: official ComfyUI worker, no models baked in
FROM runpod/worker-comfyui:5.5.0-base

ENV DEBIAN_FRONTEND=noninteractive

# 2. Small quality-of-life tools (optional, but handy)
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq curl wget git \
    && rm -rf /var/lib/apt/lists/*

# 3. Install XLabs / Flux custom nodes the supported way
#    (These live on the image; models live on the /runpod-volume network volume.)
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# 4. Copy our custom RunPod handler into the image.
#    IMPORTANT: we give it a UNIQUE module name (iw_handler.py) so we don't
#    clash with the base image's built-in /handler.py.
WORKDIR /workspace
COPY handler.py /workspace/iw_handler.py

# Make sure Python can import iw_handler, and tell RunPod to use it.
ENV PYTHONPATH="/workspace:${PYTHONPATH}"
ENV RUNPOD_HANDLER_MODULE=iw_handler:handler

# 5. Wire ComfyUI's model + input paths to the network volume.
#    On this base image, the Comfy root is /comfyui and the volume is mounted
#    at /runpod-volume (see container logs: "Adding extra search path ... /runpod-volume/...").
#
#    We:
#      - Symlink /comfyui/models/xlabs/ipadapters -> /runpod-volume/models/xlabs/ipadapters
#      - Symlink /comfyui/input -> /runpod-volume/ComfyUI/input
#
#    so that:
#      - LoadFluxIPAdapter sees ip_adapter.safetensors under models/xlabs/ipadapters
#      - LoadImage("ip_ref.png") finds the file in the volume-backed input dir.
RUN mkdir -p /comfyui/models/xlabs \
    && ln -s /runpod-volume/models/xlabs/ipadapters /comfyui/models/xlabs/ipadapters || true \
    && mkdir -p /runpod-volume/ComfyUI/input \
    && rm -rf /comfyui/input \
    && ln -s /runpod-volume/ComfyUI/input /comfyui/input || true

# 6. Do NOT override the entrypoint or healthcheck.
#    The base image already:
#      - runs ComfyUI
#      - exposes the serverless handler loop
#      - knows how to talk to RunPod's queue system.
#
#    Models will be picked up from /runpod-volume/models when your
#    network volume is mounted there.
