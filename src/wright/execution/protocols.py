"""Backward compatibility shim for execution.protocols."""

import importlib
import sys

from ..infrastructure.runtime.protocols import ExecutionBackend, ProcessHandle

_mod = importlib.import_module("wright.infrastructure.runtime.protocols")
sys.modules[__name__] = _mod

__all__ = ["ExecutionBackend", "ProcessHandle"]
