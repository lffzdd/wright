"""Backward-compatibility facade for semantic/fact memory tools (migrated to .memory.fact_tools)."""

from __future__ import annotations

from .memory.fact_tools import (
    build_memory_tools,
    create_memory,
    delete_memory,
    get_memory,
    search_memory,
    update_memory,
)

__all__ = [
    "build_memory_tools",
    "create_memory",
    "delete_memory",
    "get_memory",
    "search_memory",
    "update_memory",
]
