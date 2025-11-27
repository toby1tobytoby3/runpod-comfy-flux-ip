"""Ensure ``DoubleStreamBlock.forward`` tolerates new kwargs from newer Flux call-sites.

ComfyUI Flux variants sometimes call ``DoubleStreamBlock.forward`` with extra
keyword arguments (``attn_mask`` / ``attention_mask`` / ``transformer_options``)
that older checkpoints do not accept. This custom node:

- Patches every loaded ``DoubleStreamBlock`` (``comfy.ldm.flux.model`` and any
  vendored "flux.model" modules) so unexpected kwargs are *dropped* instead of
  raising ``TypeError``.
- Installs a meta-path import hook to reapply the patch to any future
  ``flux.model`` imports.
- Emits a one-time log of the kwargs received to speed up triage if new keys
  start appearing.
"""

import importlib
import importlib.abc
import importlib.machinery
import inspect
import logging
import os
import sys

log = logging.getLogger("flux_double_stream_patch")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)

PATCH_FLAG = "_flux_attn_mask_patched"
LOGGED_ONCE = False


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

    sig = inspect.signature(original_forward)
    has_var_kw = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())
    allowed_kwargs = {
        name
        for name, param in sig.parameters.items()
        if param.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }

    def patched_forward(self, *args, **kwargs):
        # Log once so we know which kwargs are hitting the patched forward
        global LOGGED_ONCE
        if not LOGGED_ONCE:
            try:
                log.warning(
                    "flux_double_stream_patch: DoubleStreamBlock.forward called with kwargs=%s (args_len=%d)",
                    list(kwargs.keys()),
                    len(args),
                )
            except Exception:
                pass
            LOGGED_ONCE = True

        # Drop stray kwargs that the original forward does not accept to avoid TypeErrors
        if not has_var_kw:
            for key in list(kwargs.keys()):
                if key not in allowed_kwargs:
                    kwargs.pop(key, None)
        else:
            # Even if **kwargs is accepted, remove the known problematic extras
            for bad_key in ("attn_mask", "attention_mask", "transformer_options"):
                if bad_key in kwargs:
                    log.warning(
                        "flux_double_stream_patch: dropping unsupported kwarg %r from DoubleStreamBlock.forward",
                        bad_key,
                    )
                    kwargs.pop(bad_key, None)
        # Also ensure these keys are stripped if present in the expected set
        for bad_key in ("attn_mask", "attention_mask"):
            if bad_key in kwargs:
                kwargs.pop(bad_key, None)

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
