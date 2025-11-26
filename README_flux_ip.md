new readme:

cat > comfy/README_flux_ip.md << 'EOF'
# Flux + IP-Adapter Setup (RunPod / ComfyUI)

This documents the known-good setup for Flux dev + XLabs IP-Adapter on our RunPod serverless image.

## Models & Paths

The ComfyUI container expects:

- **Flux UNet**
  - Path: `/workspace/models/unet/flux1-dev.safetensors`
  - Loaded by `UNETLoader` node (`unet_name: flux1-dev.safetensors`)

- **Flux VAE**
  - Path: `/workspace/models/vae/ae.safetensors`
  - Loaded by `VAELoader` node (`vae_name: ae.safetensors`)

- **Text encoders (DualCLIP)**
  - `clip/clip_l.safetensors`
  - `clip/t5xxl_fp8_e4m3fn.safetensors`
  - These are in `ComfyUI/models/clip/` (or the equivalent symlinked folder)
  - Loaded by `DualCLIPLoader` with:
    - `clip_name1: clip/clip_l.safetensors`
    - `clip_name2: clip/t5xxl_fp8_e4m3fn.safetensors`
    - `type: flux`
    - `device: default`

- **IP Adapter**
  - Path: `/workspace/ComfyUI/models/xlabs/ipadapters/ip_adapter.safetensors`
  - From: `XLabs-AI/flux-ip-adapter-v2` on Hugging Face
  - Loaded by `LoadFluxIPAdapter`:
    - `ipadatper: ip_adapter.safetensors`

- **CLIP Vision for IP Adapter**
  - Path: `/workspace/ComfyUI/models/clip_vision/model.safetensors`
  - From: `openai/clip-vit-large-patch14` → `model.safetensors`
  - `LoadFluxIPAdapter` uses:
    - `clip_vision: model.safetensors`

## Node IDs in pod_ip_prompt.json

The workflow `comfy/pod_ip_prompt.json` is our "canonical" Flux+IP test graph. Important nodes:

- `20` – **LoadFluxIPAdapter**
  - Inputs: `ipadatper`, `clip_vision`, `provider`
- `21` – **LoadImage**
  - Loads `ip_ref.png` from `ComfyUI/input/`
- `38` – **UNETLoader**
  - Loads `flux1-dev.safetensors`
- `39` – **VAELoader**
  - Loads `ae.safetensors`
- `40` – **DualCLIPLoader**
  - Loads `clip/clip_l.safetensors` and `clip/t5xxl_fp8_e4m3fn.safetensors`
- `41` – **CLIPTextEncodeFlux** (positive conditioning)
- `42` – **ConditioningZeroOut** (negative conditioning from 41)
- `27` – **EmptySD3LatentImage**
  - Latent size (width/height/batch)
- `31` – **KSampler**
  - Sampler, seed, steps, cfg, scheduler
- `8` – **VAEDecode**
  - Decode latents → image
- `9` – **SaveImage**
  - Saves with filename prefix `flux-ip-pod-test`

These node IDs are what we patch from the serverless handler (prompt text, IP image filename, seed, size, etc.).

## DoubleStreamBlock patch

Flux in this Comfy version can call `DoubleStreamBlock.forward(attn_mask=...)`, but the original implementation does not accept an `attn_mask` kwarg. That produced:

> TypeError: DoubleStreamBlock.forward() got an unexpected keyword argument 'attn_mask'

We fix this with a tiny patch module:

- `comfy/flux_double_stream_patch.py`

This is mounted into the container at:

- `/workspace/ComfyUI/custom_nodes/flux_double_stream_patch.py`

It:

- imports `comfy.ldm.flux.model.DoubleStreamBlock`
- wraps its `forward()` to accept an `attn_mask` kwarg (and drop it)
- prints a log line when active

Without this patch, Flux+IP workflows may crash with the `attn_mask` TypeError.

### Why some runs save images and others do not

ComfyUI launches a separate Python process for the UI/runtime. When that
process imports `comfy.ldm.flux.model` **before** our patch module is on
`sys.meta_path`, the `DoubleStreamBlock` remains unpatched and Flux fails with
`attn_mask` during sampling. That failure happens inside the diffusion loop, so
the workflow finishes without saving anything. When the patch loads **before**
the first Flux import, the `attn_mask` argument is dropped and the run
completes, producing images.

To make the patch consistent across processes and import orders we now ship
`flux_double_stream_patch.py` as a Comfy custom node that executes its patch
logic on import. When ComfyUI loads custom nodes, the module:

1. Patches `DoubleStreamBlock.forward` immediately if `comfy.ldm.flux.model`
   was already imported.
2. Otherwise registers an import hook that applies the patch the first time the
   module is loaded.

Because the patch runs inside the actual ComfyUI process, the `attn_mask` bug
is avoided regardless of startup sequence, and Flux generations save
consistently.

### Which patch variant to keep

Use the import-on-load custom-node version. It guarantees the patch is applied
inside the ComfyUI runtime regardless of import timing, so Flux generations are
consistently saved. Older variants that relied on a deferred hook alone could
miss the first import and lead to intermittent failures.

### Extra guard via `sitecustomize.py`

We also ship a small `sitecustomize.py` that is copied into the virtual
environment (`/opt/venv/lib/python3.12/site-packages/sitecustomize.py`). Python
imports this automatically on interpreter startup, so it pre-pends the custom
node directory to `sys.path` and eagerly imports `flux_double_stream_patch`. If
ComfyUI ever starts before loading custom nodes, this guarantees the patch is
still active in the process that actually runs Flux.

## Summary

- `comfy/pod_ip_prompt.json` = golden Flux+IP workflow template
- `comfy/flux_double_stream_patch.py` = required patch for `DoubleStreamBlock.forward(attn_mask=...)`
- These files must be copied into the image and into the right Comfy paths at build time in the Dockerfile.
EOF
