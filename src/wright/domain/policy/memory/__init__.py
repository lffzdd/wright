"""Domain policies for agent memory (Core, Fact, Episodic)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import CoreMemoryPolicy
from .episode import EpisodePolicy
from .fact import FactPolicy, is_safe_fact


@dataclass(frozen=True)
class MemoryPolicy:
    """Unified policy facade for memory management."""

    fact: FactPolicy = field(default_factory=FactPolicy)
    episode: EpisodePolicy = field(default_factory=EpisodePolicy)
    core: CoreMemoryPolicy = field(default_factory=CoreMemoryPolicy)


__all__ = [
    "CoreMemoryPolicy",
    "EpisodePolicy",
    "FactPolicy",
    "MemoryPolicy",
    "is_safe_fact",
]
