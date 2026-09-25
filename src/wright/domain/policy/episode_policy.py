"""Backward-compatibility facade for EpisodePolicy (migrated to .memory.episode)."""

from __future__ import annotations

from .memory.episode import EpisodePolicy

__all__ = ["EpisodePolicy"]
