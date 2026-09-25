"""Backward-compatibility facade for Fact models (migrated to .memory.fact)."""

from __future__ import annotations

from .memory.fact import (
    FRONTMATTER_EXAMPLE,
    Fact,
    FactCategory,
    FactScope,
    MEMORY_TYPES,
    MemoryAlreadyExistsError,
    MemoryHeader,
    MemoryNotFoundError,
    MemoryRecord,
    MemoryStoreError,
    MemoryType,
    TRUSTING_RECALL,
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE,
    WHEN_TO_ACCESS,
    parse_memory_type,
)

__all__ = [
    "FRONTMATTER_EXAMPLE",
    "Fact",
    "FactCategory",
    "FactScope",
    "MEMORY_TYPES",
    "MemoryAlreadyExistsError",
    "MemoryHeader",
    "MemoryNotFoundError",
    "MemoryRecord",
    "MemoryStoreError",
    "MemoryType",
    "TRUSTING_RECALL",
    "TYPES_SECTION",
    "WHAT_NOT_TO_SAVE",
    "WHEN_TO_ACCESS",
    "parse_memory_type",
]
