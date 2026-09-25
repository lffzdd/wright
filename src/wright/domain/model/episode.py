"""Backward-compatibility facade for Episode models (migrated to .memory.episode)."""

from __future__ import annotations

from .memory.episode import (
    MAX_EPISODE_AGENTS,
    MAX_EPISODE_GOAL_CHARS,
    MAX_EPISODE_OUTCOME_CHARS,
    MAX_EPISODE_TOOLS,
    MAX_EPISODE_VERIFICATIONS,
    Episode,
    EpisodeNotFoundError,
    EpisodeOutcome,
    EpisodeRecord,
    EpisodeStatus,
    EpisodeStoreError,
)

__all__ = [
    "MAX_EPISODE_AGENTS",
    "MAX_EPISODE_GOAL_CHARS",
    "MAX_EPISODE_OUTCOME_CHARS",
    "MAX_EPISODE_TOOLS",
    "MAX_EPISODE_VERIFICATIONS",
    "Episode",
    "EpisodeNotFoundError",
    "EpisodeOutcome",
    "EpisodeRecord",
    "EpisodeStatus",
    "EpisodeStoreError",
]
