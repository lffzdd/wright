"""Backward-compatibility facade for FileCoreMemoryStore (migrated to .memory.core)."""

from __future__ import annotations

from .memory.core import CORE_MEMORY_FILE, FileCoreMemoryStore

__all__ = ["CORE_MEMORY_FILE", "FileCoreMemoryStore"]
