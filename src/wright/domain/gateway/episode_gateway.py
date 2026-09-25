"""Backward-compatibility facade for IEpisodicMemoryStore (migrated to .memory.episode)."""

from __future__ import annotations

from .memory.episode import IEpisodicMemoryStore

__all__ = ["IEpisodicMemoryStore"]
