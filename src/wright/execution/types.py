"""Backward compatibility shim for execution.types."""

import importlib
import sys

from ..infrastructure.runtime.types import (
    CompletedProcess,
    DirectoryEntry,
    ExecutionPath,
    FileKind,
    FileMetadata,
    SearchMatch,
)

_mod = importlib.import_module("wright.infrastructure.runtime.types")
sys.modules[__name__] = _mod

__all__ = [
    "CompletedProcess",
    "DirectoryEntry",
    "ExecutionPath",
    "FileKind",
    "FileMetadata",
    "SearchMatch",
]
