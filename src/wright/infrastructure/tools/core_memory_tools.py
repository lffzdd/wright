"""Backward-compatibility facade for core memory tools (migrated to .memory.core_tools)."""

from __future__ import annotations

from .memory.core_tools import (
    build_core_memory_tools,
    get_core_memory,
    update_core_memory,
)

__all__ = [
    "build_core_memory_tools",
    "get_core_memory",
    "update_core_memory",
]
