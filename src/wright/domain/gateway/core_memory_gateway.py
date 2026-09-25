"""Backward-compatibility facade for ICoreMemoryStore (migrated to .memory.core)."""

from __future__ import annotations

from .memory.core import ICoreMemoryStore

__all__ = ["ICoreMemoryStore"]
