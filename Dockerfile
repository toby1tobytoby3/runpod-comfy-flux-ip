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
# Flux DoubleStreamBlock patch — deferred import-safe version
# ---------------------------------------------------------------------
RUN python - <<'PY'
import pathlib, textwrap
p = pathlib.Path("/comfyui/custom_nodes/flux_double_stream_patch.py")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(textwrap.dedent("""
    import sys, importlib, logging
    log = logging.getLogger(__name__)

    def _apply_patch():
        try:
            import comfy.ldm.flux.model as flux_model
            from comfy.ldm.flux.model import DoubleStreamBlock
            original_forward = DoubleStreamBlock.forward
            def patched_forward(self, *args, **kwargs):
                kwargs.pop("attn_mask", None)
                return original_forward(self, *args, **kwargs)
            DoubleStreamBlock.forward = patched_forward
            log.info("✅ flux_double_stream_patch: DoubleStreamBlock.forward patched successfully (deferred)")
        except Exception as e:
            log.warning("⚠️ flux_double_stream_patch: could not apply patch: %s", e)

    class _FluxImportHook:
        def find_spec(self, fullname, path, target=None):
            if fullname == "comfy.ldm.flux.model":
                importlib.invalidate_caches()
                spec = importlib.util.find_spec(fullname)
                if spec and not hasattr(spec, "_flux_patch_wrapped"):
                    orig_loader = spec.loader
                    class LoaderWrapper(orig_loader.__class__):
                        def exec_module(self, module):
                            orig_loader.exec_module(module)
                            try:
                                _apply_patch()
                            except Exception as e:
                                log.warning("⚠️ deferred patch failed: %s", e)
                    spec.loader = LoaderWrapper()
                    spec._flux_patch_wrapped = True
                return spec
            return None

    sys.meta_path.insert(0, _FluxImportHook())

    NODE_CLASS_MAPPINGS = {}
    NODES_LIST = []
"""))
print("Wrote /comfyui/custom_nodes/flux_double_stream_patch.py (deferred patch)")
PY

# ---------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------
RUN pip install --no-cache-dir runpod requests

# ---------------------------------------------------------------------
# Bootstrap & Launch
# ---------------------------------------------------------------------
# Run the patch bootstrap BEFORE handler starts ComfyUI.
CMD ["bash", "-c", "python3 /comfyui/custom_nodes/flux_double_stream_patch.py && python3 /workspace/handler.py"]
