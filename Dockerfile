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

# 6. Patch ComfyUI's CLIP model to handle resized vision inputs (Flux IP-Adapter)
#    This fixes the tensor size mismatch: 256 vs 729 at non-singleton dimension 1
#    by interpolating positional embeddings to match the token grid.
RUN python - << 'PY'
import os

path = "/comfyui/comfy/clip_model.py"
with open(path, "r", encoding="utf-8") as f:
    src = f.read()

needle = "        return embeds + comfy.ops.cast_to_input(self.position_embedding.weight, embeds)\n"

if needle not in src:
    raise SystemExit("Patch needle not found in clip_model.py – file layout changed?")

replacement = """        # Patched to support resized vision inputs (e.g. Flux IP-Adapter)
        pos = self.position_embedding.weight  # [N_pos, D]
        # embeds: [B, N_tokens, D]
        if pos.shape[0] != embeds.shape[1]:
            import math
            import torch
            import torch.nn.functional as F

            n_tokens = embeds.shape[1]
            n_pos = pos.shape[0]

            # Assume first token is CLS, remaining form an HxW grid
            cls_pos = pos[:1]
            patch_pos = pos[1:]

            old_n = patch_pos.shape[0]
            new_n = max(n_tokens - 1, 1)

            old_size = int(math.sqrt(old_n))
            new_size = int(math.sqrt(new_n))

            if old_size * old_size == old_n and new_size * new_size == new_n:
                # reshape to [1, D, H, W], interpolate, reshape back
                patch_pos = patch_pos.reshape(1, old_size, old_size, -1).permute(0, 3, 1, 2)
                patch_pos = F.interpolate(
                    patch_pos,
                    size=(new_size, new_size),
                    mode="bicubic",
                    align_corners=False,
                )
                patch_pos = patch_pos.permute(0, 2, 3, 1).reshape(new_n, -1)
                pos = torch.cat([cls_pos, patch_pos], dim=0)
            else:
                # Fallback: truncate or pad to match token count
                if n_pos > n_tokens:
                    pos = pos[:n_tokens]
                else:
                    pad = pos[0:1].expand(n_tokens - n_pos, -1)
                    pos = torch.cat([pos, pad], dim=0)

        return embeds + comfy.ops.cast_to_input(pos, embeds)\n"""

src = src.replace(needle, replacement)

with open(path, "w", encoding="utf-8") as f:
    f.write(src)
PY

# 7. Do NOT override the entrypoint or healthcheck.
#    The base image already:
#      - runs ComfyUI
#      - exposes the serverless handler loop
#      - knows how to talk to RunPod's queue system.
#
#    Models will be picked up from /runpod-volume/models when your
#    network volume is mounted there.
