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

## Summary

- `comfy/pod_ip_prompt.json` = golden Flux+IP workflow template
- `comfy/flux_double_stream_patch.py` = required patch for `DoubleStreamBlock.forward(attn_mask=...)`
- These files must be copied into the image and into the right Comfy paths at build time in the Dockerfile.
EOF
