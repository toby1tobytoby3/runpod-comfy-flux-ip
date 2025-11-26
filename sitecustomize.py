"""Global bootstrap to ensure Comfy custom patch loads in every process.

This module is auto-imported by Python when present on sys.path. We use it to
make sure `/comfyui/custom_nodes` is importable and to eagerly import the Flux
patch module so DoubleStreamBlock.forward is patched even before ComfyUI loads
custom nodes.
"""
from __future__ import annotations
import importlib
import logging
import pathlib
import sys

log = logging.getLogger("sitecustomize_flux_patch")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
log.setLevel(logging.INFO)

CUSTOM_NODE_DIR = pathlib.Path("/comfyui/custom_nodes")
if CUSTOM_NODE_DIR.exists():
    str_path = str(CUSTOM_NODE_DIR)
    if str_path not in sys.path:
        sys.path.insert(0, str_path)
        log.info("sitecustomize: added %%s to sys.path", str_path)
else:
    log.warning("sitecustomize: custom node dir missing: %%s", CUSTOM_NODE_DIR)

try:
    importlib.import_module("flux_double_stream_patch")
    log.info("sitecustomize: flux_double_stream_patch imported")
except Exception as exc:  # pragma: no cover - defensive
    log.warning("sitecustomize: failed to import flux_double_stream_patch: %%s", exc)
