"""
Minimal, robust patch for Flux DoubleStreamBlock.forward attn_mask mismatch.

This runs as a ComfyUI custom node module. On import, it:

- Imports comfy.ldm.flux.model (which defines Flux + DoubleStreamBlock)
- Wraps DoubleStreamBlock.forward so it *ignores* any unexpected `attn_mask`
  keyword, avoiding TypeError when newer Flux call sites pass that argument.
"""

import logging

log = logging.getLogger("flux_double_stream_patch")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)

PATCH_FLAG = "_flux_attn_mask_patched"


def _apply_patch() -> bool:
    try:
        # This will use the comfy copy of Flux.
        import comfy.ldm.flux.model as flux_model  # type: ignore

        DoubleStreamBlock = flux_model.DoubleStreamBlock
        original_forward = DoubleStreamBlock.forward
    except Exception as exc:
        log.warning(
            "⚠️ flux_double_stream_patch: could not import Flux DoubleStreamBlock: %s",
            exc,
        )
        return False

    # Avoid double-wrapping if Comfy reloads the node.
    if getattr(original_forward, PATCH_FLAG, False):
        log.info(
            "flux_double_stream_patch: DoubleStreamBlock.forward already patched, skipping"
        )
        return True

    def patched_forward(self, *args, **kwargs):
        # Drop stray attn_mask kwarg if present.
        if "attn_mask" in kwargs:
            kwargs.pop("attn_mask", None)
        return original_forward(self, *args, **kwargs)

    setattr(patched_forward, PATCH_FLAG, True)
    DoubleStreamBlock.forward = patched_forward
    log.info(
        "✅ flux_double_stream_patch: DoubleStreamBlock.forward patched via direct import"
    )
    return True


# Run the patch as soon as this module is imported by ComfyUI.
_apply_patch()

# No actual nodes are added; this file exists purely for its side-effect.
NODE_CLASS_MAPPINGS = {}
NODES_LIST = []
