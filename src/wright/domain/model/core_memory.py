"""Backward-compatibility facade for CoreMemory (migrated to .memory.core)."""

from __future__ import annotations

from .memory.core import (
    DEFAULT_HUMAN_PROFILE,
    DEFAULT_PERSONA,
    DEFAULT_PROJECT_ANCHOR,
    CoreMemory,
)

__all__ = [
    "DEFAULT_HUMAN_PROFILE",
    "DEFAULT_PERSONA",
    "DEFAULT_PROJECT_ANCHOR",
    "CoreMemory",
]
