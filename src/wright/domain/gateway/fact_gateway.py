"""Backward-compatibility facade for IFactRepository (migrated to .memory.fact)."""

from __future__ import annotations

from .memory.fact import IFactRepository

__all__ = ["IFactRepository"]
