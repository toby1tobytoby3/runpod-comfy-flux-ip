"""Ensure Flux DoubleStreamBlock.forward ignores unexpected attn_mask args.

ComfyUI's Flux models can receive an ``attn_mask`` keyword that older Flux
weights don't accept. This script applies a defensive patch even if
``comfy.ldm.flux.model`` was imported before the custom node is loaded, while
also registering an import hook as a fallback.
"""
import importlib
import logging
import sys
from types import ModuleType

log = logging.getLogger("flux_double_stream_patch")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)


PATCH_FLAG = "_flux_attn_mask_patched"


def _patch_forward(module: ModuleType, *, reason: str) -> bool:
    """Patch DoubleStreamBlock.forward to drop attn_mask if present."""
    try:
        DoubleStreamBlock = module.DoubleStreamBlock
        original_forward = DoubleStreamBlock.forward
        if getattr(original_forward, PATCH_FLAG, False):
            log.info("flux_double_stream_patch: forward already patched (%s)", reason)
            return True

        def patched_forward(self, *args, **kwargs):
            kwargs.pop("attn_mask", None)
            return original_forward(self, *args, **kwargs)

        setattr(patched_forward, PATCH_FLAG, True)
        DoubleStreamBlock.forward = patched_forward
        log.info(
            "✅ flux_double_stream_patch: DoubleStreamBlock.forward patched successfully (%s)",
            reason,
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive logging only
        log.warning("⚠️ flux_double_stream_patch: could not apply patch (%s): %s", reason, exc)
        return False


def _ensure_patch_applied() -> bool:
    # If the module is already loaded, patch immediately.
    module = sys.modules.get("comfy.ldm.flux.model")
    if isinstance(module, ModuleType):
        return _patch_forward(module, reason="module already imported")

    class _FluxImportHook:
        def find_spec(self, fullname, path, target=None):
            if fullname != "comfy.ldm.flux.model":
                return None
            spec = importlib.util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return None
            original_loader = spec.loader

            class LoaderWrapper(original_loader.__class__):
                def exec_module(self, module):
                    original_loader.exec_module(module)
                    _patch_forward(module, reason="import hook")

            spec.loader = LoaderWrapper()
            return spec

    # Avoid inserting duplicate hooks when the node reloads.
    if not any(isinstance(hook, _FluxImportHook) for hook in sys.meta_path):
        sys.meta_path.insert(0, _FluxImportHook())
        log.info("flux_double_stream_patch: registered import hook for comfy.ldm.flux.model")
    return False


NODE_CLASS_MAPPINGS = {}
NODES_LIST = []


if __name__ == "__main__":
    _ensure_patch_applied()
