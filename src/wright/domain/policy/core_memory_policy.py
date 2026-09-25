"""Backward-compatibility facade for CoreMemoryPolicy (migrated to .memory.core)."""

from __future__ import annotations

from .memory.core import CoreMemoryPolicy

__all__ = ["CoreMemoryPolicy"]
