"""
Minimal, robust patch for Flux DoubleStreamBlock.forward attn_mask mismatch.

This runs as a ComfyUI custom node module. On import, it:

- Imports comfy.ldm.flux.model (which defines Flux + DoubleStreamBlock)
- Wraps DoubleStreamBlock.forward so it *ignores* any unexpected `attn_mask`
  keyword, avoiding TypeError when newer Flux call sites pass that argument.
- Additionally scans all loaded modules whose name contains "flux.model"
  and patches any extra DoubleStreamBlock definitions (e.g. variants
  coming from x-flux-comfyui or other custom loaders).
"""

import importlib
import importlib.abc
import importlib.machinery
import logging
import os
import sys

log = logging.getLogger("flux_double_stream_patch")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)

PATCH_FLAG = "_flux_attn_mask_patched"


def _patch_module(mod) -> bool:
    """
    Given a module object, try to patch its DoubleStreamBlock.forward.
    Returns True if a new patch was applied, False otherwise.
    """
    name = getattr(mod, "__name__", str(mod))

    try:
        DoubleStreamBlock = mod.DoubleStreamBlock
        original_forward = DoubleStreamBlock.forward
    except Exception as exc:  # noqa: BLE001
        # Not a flux model module, or missing DoubleStreamBlock.
        log.debug(
            "flux_double_stream_patch: %s has no DoubleStreamBlock (%s)",
            name,
            exc,
        )
        return False

    # Avoid double-wrapping if Comfy reloads the node or module.
    if getattr(original_forward, PATCH_FLAG, False):
        log.info(
            "flux_double_stream_patch: %s.DoubleStreamBlock.forward already patched, skipping",
            name,
        )
        return False

    def patched_forward(self, *args, **kwargs):
        # Drop stray attn_mask kwarg if present.
        if "attn_mask" in kwargs:
            kwargs.pop("attn_mask", None)
        return original_forward(self, *args, **kwargs)

    setattr(patched_forward, PATCH_FLAG, True)
    DoubleStreamBlock.forward = patched_forward

    log.info(
        "✅ flux_double_stream_patch: patched DoubleStreamBlock.forward in %s",
        name,
    )
    return True


def _apply_patch_all() -> int:
    """
    Patch the canonical comfy.ldm.flux.model *and* any additional
    Flux model modules that are already loaded into sys.modules.

    Returns the number of modules we actually patched.
    """
    patched = 0

    # 1) Patch the canonical comfy module first (base Flux).
    try:
        import comfy.ldm.flux.model as flux_model  # type: ignore[attr-defined]

        if _patch_module(flux_model):
            patched += 1
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "⚠️ flux_double_stream_patch: could not import comfy.ldm.flux.model: %s",
            exc,
        )

    # 2) Patch any *additional* flux.model modules already loaded.
    #    Some custom nodes may import Flux under different module names
    #    but from the same or a vendored file.
    for name, mod in list(sys.modules.items()):
        if not isinstance(name, str):
            continue
        if "flux.model" not in name:
            continue
        if mod is None:
            continue
        if not hasattr(mod, "DoubleStreamBlock"):
            continue

        if _patch_module(mod):
            patched += 1

    log.info("flux_double_stream_patch: total modules patched: %d", patched)
    return patched


class _FluxImportHook(importlib.abc.MetaPathFinder):
    """Meta path finder that patches Flux modules as they are imported.

    Some custom nodes reload or vend their own copies of ``flux.model``.
    Installing this hook guarantees we re-apply the ``attn_mask`` patch to
    every new ``DoubleStreamBlock`` definition as soon as it is loaded.
    """

    def find_spec(self, fullname, path, target=None):  # noqa: D401
        if "flux.model" not in fullname:
            return None

        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if not spec or not spec.loader or not hasattr(spec.loader, "exec_module"):
            return spec

        orig_exec_module = spec.loader.exec_module

        def _exec_and_patch(module):
            orig_exec_module(module)
            try:
                _patch_module(module)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "⚠️ flux_double_stream_patch: import hook could not patch %s: %s",
                    fullname,
                    exc,
                )

        spec.loader.exec_module = _exec_and_patch
        return spec


def _install_import_hook():
    if any(isinstance(f, _FluxImportHook) for f in sys.meta_path):
        return False
    sys.meta_path.insert(0, _FluxImportHook())
    log.info("flux_double_stream_patch: import hook installed")
    return True


# Optional debug helper to enumerate loaded Flux modules for quicker triage.
def _log_loaded_flux_modules():
    log.info("flux_double_stream_patch: listing loaded flux modules...")
    for name in sorted(sys.modules.keys()):
        if "flux.model" in name:
            log.info("  - %s", name)


# Run the patch as soon as this module is imported by ComfyUI.
_apply_patch_all()
# And keep patching future imports/reloads of any flux.model variants.
_install_import_hook()
# If requested, emit the currently loaded flux modules to the log to help
# pinpoint which names the import hook should target.
if os.getenv("FLUX_PATCH_LOG_MODULES"):
    _log_loaded_flux_modules()

# No actual nodes are added; this file exists purely for its side-effect.
NODE_CLASS_MAPPINGS = {}
NODES_LIST = []
