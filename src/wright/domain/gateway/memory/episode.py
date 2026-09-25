"""Episodic memory store port for experience case study retrieval and append-only persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...model.memory import Episode


class IEpisodicMemoryStore(ABC):
    """Port for searching similar experience episodes and appending new ones."""

    @abstractmethod
    def search_episodes(self, query: str, top_k: int = 3) -> Sequence[tuple[Any, float]]:
        """Search episodes by similarity to given query, returning (episode, score) tuples."""
        ...

    @abstractmethod
    def record_episode(self, episode: Any) -> None:
        """Append an episodic case study."""
        ...


__all__ = ["IEpisodicMemoryStore"]
