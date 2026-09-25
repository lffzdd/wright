"""Domain gateway ports for memory subsystems (Core, Fact, Episodic)."""

from __future__ import annotations

from .core import ICoreMemoryStore
from .episode import IEpisodicMemoryStore
from .fact import IFactRepository

__all__ = [
    "ICoreMemoryStore",
    "IEpisodicMemoryStore",
    "IFactRepository",
]
