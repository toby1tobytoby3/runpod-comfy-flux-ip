# 1. Base: official ComfyUI worker, no models baked in
FROM runpod/worker-comfyui:5.5.0-base

ENV DEBIAN_FRONTEND=noninteractive

# 2. Small quality-of-life tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq curl wget git \
    && rm -rf /var/lib/apt/lists/*

# 3. Install XLabs / Flux custom nodes
RUN comfy-node-install https://github.com/XLabs-AI/x-flux-comfyui || true

# 4. Copy our custom handler into place
WORKDIR /workspace
COPY handler.py /workspace/handler.py

# 5. Wire ComfyUI model + IO paths to the network volume
#    NOTE: in the serverless endpoint, the NFS volume is mounted at /runpod-volume.
#    In the dev pod you see the SAME volume at /workspace.
RUN mkdir -p /comfyui/models/xlabs \
    && ln -s /runpod-volume/models/xlabs/ipadapters /comfyui/models/xlabs/ipadapters || true \
    && mkdir -p /runpod-volume/ComfyUI/input \
    && mkdir -p /runpod-volume/ComfyUI/output \
    && rm -rf /comfyui/input /comfyui/output \
    && ln -s /runpod-volume/ComfyUI/input /comfyui/input || true \
    && ln -s /runpod-volume/ComfyUI/output /comfyui/output || true

# 6. Patch CLIP model to handle resized vision inputs (Flux IP Adapter fix)
RUN python - << 'PY'
import os
path = "/comfyui/comfy/clip_model.py"
with open(path, "r", encoding="utf-8") as f:
    src = f.read()
needle = "        return embeds + comfy.ops.cast_to_input(self.position_embedding.weight, embeds)\n"
replacement = """        # Patched to support resized vision inputs (e.g. Flux IP-Adapter)
        pos = self.position_embedding.weight
        if pos.shape[0] != embeds.shape[1]:
            import math, torch, torch.nn.functional as F
            n_tokens, n_pos = embeds.shape[1], pos.shape[0]
            cls_pos, patch_pos = pos[:1], pos[1:]
            old_n, new_n = patch_pos.shape[0], max(n_tokens - 1, 1)
            old_s, new_s = int(math.sqrt(old_n)), int(math.sqrt(new_n))
            if old_s * old_s == old_n and new_s * new_s == new_n:
                patch_pos = patch_pos.reshape(1, old_s, old_s)
                patch_pos = F.interpolate(patch_pos.permute(0, 3, 1, 2),
                                          size=(new_s, new_s),
                                          mode="bicubic",
                                          align_corners=False)
                patch_pos = patch_pos.permute(0, 2, 3, 1).reshape(1, new_n, -1)
                pos = torch.cat([cls_pos, patch_pos], dim=1)[0]
            else:
                # simple pad / crop if shapes don't line up as a square grid
                if n_tokens < n_pos:
                    pos = pos[:n_tokens]
                else:
                    pad = pos[0:1].expand(n_tokens - n_pos, -1)
                    pos = torch.cat([pos, pad], dim=0)
        return embeds + comfy.ops.cast_to_input(pos, embeds)\n"""
if needle in src:
    src = src.replace(needle, replacement)
    with open(path, "w", encoding="utf-8") as f: f.write(src)
PY

# 6b. Patch DoubleStreamBlock.forward to handle attn_mask (Flux compatibility fix)
RUN python - << 'PY'
import re, os
path = "/comfyui/comfy/ldm/flux/model.py"
if os.path.exists(path):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    # Look for the DoubleStreamBlock definition
    if "class DoubleStreamBlock" in src and "def forward" in src:
        pattern = r"def forward\(self, ([^\)]*)\):"
        repl = (
            "def forward(self, \\1, attn_mask=None):"
        )
        src2 = re.sub(pattern, repl, src, count=1)
        # Add compatibility handling for attn_mask inside the method
        if "attn_mask" not in src2.split("def forward")[1]:
            insert = (
                "\n        # Added patch: safely ignore attn_mask if passed by newer Comfy samplers\n"
                "        if 'attn_mask' in kwargs:\n"
                "            kwargs.pop('attn_mask', None)\n"
            )
            src2 = src2.replace("def forward", "def forward" + insert, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src2)
PY

# 7. Install RunPod SDK (for health + job loop)
RUN pip install --no-cache-dir runpod requests

# 8. Entrypoint
CMD ["python3", "/workspace/handler.py"]
