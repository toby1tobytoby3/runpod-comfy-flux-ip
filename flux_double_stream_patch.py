import builtins
import inspect
import logging
import sys
from types import ModuleType

log = logging.getLogger(__name__)

_PATCH_SENTINEL = "_flux_double_stream_patch_applied"


def _patch_module(module: ModuleType) -> bool:
    """
    Find a DoubleStreamBlock in the given module and patch its .forward
    so that it safely ignores extra kwargs like attn_mask / transformer_options.

    Returns True if we patched anything, False otherwise.
    """
    if module is None:
        return False

    try:
        dct = getattr(module, "__dict__", None)
        if not isinstance(dct, dict):
            return False

        DoubleStreamBlock = dct.get("DoubleStreamBlock")
        if not isinstance(DoubleStreamBlock, type):
            return False

        forward = getattr(DoubleStreamBlock, "forward", None)
        if not callable(forward):
            return False

        # If we've already wrapped this forward, do nothing.
        if getattr(forward, _PATCH_SENTINEL, False):
            # Only log at DEBUG so we don't spam.
            log.debug(
                "flux_double_stream_patch: %s.DoubleStreamBlock.forward already patched, skipping",
                getattr(module, "__name__", repr(module)),
            )
            return False

        # Introspect original signature so we don't pass unexpected kwargs.
        try:
            sig = inspect.signature(forward)
            params = sig.parameters
            allow_all_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            allowed_kw = {
                name
                for name, p in params.items()
                if name != "self"
                and p.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            }
        except Exception as e:  # pragma: no cover
            log.warning(
                "flux_double_stream_patch: could not inspect signature for %r: %s",
                module,
                e,
            )
            sig = None
            allow_all_kwargs = True
            allowed_kw = set()

        original_forward = forward

        def patched_forward(self, *args, **kwargs):
            # This can be noisy, keep at DEBUG.
            log.debug(
                "flux_double_stream_patch: DoubleStreamBlock.forward called with kwargs=%s (args_len=%d)",
                list(kwargs.keys()),
                len(args),
            )

            # Comfy / x-flux now sometimes pass these, but older DoubleStreamBlock
            # implementations don't know about them. We just drop them.
            kwargs.pop("attn_mask", None)
            kwargs.pop("attention_mask", None)
            kwargs.pop("transformer_options", None)

            # If the original forward didn't accept arbitrary **kwargs,
            # filter out anything that isn't in the original signature.
            if sig is not None and not allow_all_kwargs:
                filtered = {k: v for k, v in kwargs.items() if k in allowed_kw}
            else:
                filtered = kwargs

            return original_forward(self, *args, **filtered)

        # Mark so we can detect and avoid double-wrapping later.
        setattr(patched_forward, _PATCH_SENTINEL, True)

        DoubleStreamBlock.forward = patched_forward

        log.info(
            "✅ flux_double_stream_patch: patched DoubleStreamBlock.forward in %s",
            getattr(module, "__name__", repr(module)),
        )
        return True

    except Exception as e:  # pragma: no cover
        log.exception(
            "flux_double_stream_patch: unexpected error while patching %r: %s", module, e
        )
        return False


def _patch_all_loaded_modules() -> int:
    """
    Scan sys.modules and patch any module that defines DoubleStreamBlock.
    This is cheap and robust; we also guard against double-wrapping.
    """
    count = 0
    for module in list(sys.modules.values()):
        try:
            if _patch_module(module):
                count += 1
        except Exception:  # pragma: no cover
            log.exception(
                "flux_double_stream_patch: error while scanning module %r", module
            )
    if count:
        log.info(
            "flux_double_stream_patch: total DoubleStreamBlock-containing modules patched in scan: %d",
            count,
        )
    return count


# Initial scan: catches any flux modules that were imported before this custom node.
_patch_all_loaded_modules()


# Global import hook: after every import, rescan sys.modules and patch any new
# DoubleStreamBlock definitions. We do this in the simplest, safest way by
# wrapping builtins.__import__ and re-running the scan. Double-patching is
# avoided via the sentinel on the wrapped forward.
if not getattr(builtins, "_flux_double_stream_import_hook_installed", False):
    _original_import = builtins.__import__

    def _patched_import(name, globals=None, locals=None, fromlist=(), level=0):
        module = _original_import(name, globals, locals, fromlist, level)
        # After any import completes, patch any new DoubleStreamBlock classes.
        _patch_all_loaded_modules()
        return module

    builtins.__import__ = _patched_import
    builtins._flux_double_stream_import_hook_installed = True
    log.info("flux_double_stream_patch: global import hook installed")
