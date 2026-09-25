"""Backward compatibility shim for execution.authorized."""

import importlib
import sys

from ..infrastructure.runtime.authorized import AuthorizedExecution

_mod = importlib.import_module("wright.infrastructure.runtime.authorized")
sys.modules[__name__] = _mod

__all__ = ["AuthorizedExecution"]
