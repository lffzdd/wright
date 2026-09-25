"""Backward compatibility shim for execution.local."""

import importlib
import sys

from ..infrastructure.runtime.local import LocalExecutionBackend, LocalProcessHandle

_mod = importlib.import_module("wright.infrastructure.runtime.local")
sys.modules[__name__] = _mod

__all__ = ["LocalExecutionBackend", "LocalProcessHandle"]
