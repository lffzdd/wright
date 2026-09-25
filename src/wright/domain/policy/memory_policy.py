"""Memory policy facade combining fact and episodic memory policies."""

from __future__ import annotations

from dataclasses import dataclass, field

from .episode_policy import EpisodePolicy
from .fact_policy import FactPolicy, is_safe_fact


@dataclass(frozen=True)
class MemoryPolicy:
    """Unified policy facade for memory management."""

    fact: FactPolicy = field(default_factory=FactPolicy)
    episode: EpisodePolicy = field(default_factory=EpisodePolicy)


__all__ = [
    "EpisodePolicy",
    "FactPolicy",
    "MemoryPolicy",
    "is_safe_fact",
]
