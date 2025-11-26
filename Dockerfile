FROM runpod/worker-comfyui:5.5.0-base
ENV DEBIAN_FRONTEND=noninteractive

# Core tools
RUN apt-get update && apt-get install -y --no-install-recommends jq curl wget git && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------
# Flux custom node (XLabs)
# ---------------------------------------------------------------------
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# ---------------------------------------------------------------------
# Copy handler
# ---------------------------------------------------------------------
WORKDIR /workspace
COPY handler.py /workspace/handler.py

# ---------------------------------------------------------------------
# Shared I/O and model paths
# ---------------------------------------------------------------------
ENV COMFY_DIR=/comfyui
ENV INPUT_DIR=/runpod-volume/ComfyUI/input
ENV OUTPUT_DIR=/runpod-volume/ComfyUI/output

RUN mkdir -p /runpod-volume/ComfyUI/input /runpod-volume/ComfyUI/output && \
    rm -rf /comfyui/input /comfyui/output && \
    ln -sf /runpod-volume/ComfyUI/input /comfyui/input && \
    ln -sf /runpod-volume/ComfyUI/output /comfyui/output

# Expose IP-Adapter model (both standard and XLabs layouts)
RUN mkdir -p /comfyui/models/ipadapters && \
    ln -sf /runpod-volume/models/ip_adapter/ip_adapter.safetensors /comfyui/models/ipadapters/ip_adapter.safetensors || true && \
    mkdir -p /comfyui/models/xlabs && \
    ln -sf /runpod-volume/models/xlabs/ipadapters /comfyui/models/xlabs/ipadapters || true

# ---------------------------------------------------------------------
# Flux DoubleStreamBlock patch — resilient import version
# ---------------------------------------------------------------------
COPY flux_double_stream_patch.py /comfyui/custom_nodes/flux_double_stream_patch.py

# ---------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------
RUN pip install --no-cache-dir runpod requests

# ---------------------------------------------------------------------
# Bootstrap & Launch
# ---------------------------------------------------------------------
# Run the patch bootstrap BEFORE handler starts ComfyUI.
CMD ["bash", "-c", "python3 /comfyui/custom_nodes/flux_double_stream_patch.py && python3 /workspace/handler.py"]
