"""Backward-compatibility facade for memory_paths (migrated to .memory.paths)."""

from __future__ import annotations

from .memory.paths import (
    MEMORY_INDEX,
    ensure_memory_dir,
    entrypoint_path,
    memory_dir,
)

__all__ = [
    "MEMORY_INDEX",
    "ensure_memory_dir",
    "entrypoint_path",
    "memory_dir",
]
