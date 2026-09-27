"""Episode memory store port: append and read finished-turn records."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ...model.memory import (
    EpisodeRecord,
    EpisodeSearchHit,
    EpisodeSearchScope,
    EpisodeStatus,
)


class IEpisodeStore(ABC):
    """Port for the episode log the application actually persists."""

    @abstractmethod
    def save(self, episode: EpisodeRecord) -> EpisodeRecord:
        """Append one record. A repeated id returns the stored record unchanged."""
        ...

    @abstractmethod
    def get(self, episode_id: str) -> EpisodeRecord:
        """Load one record by id."""
        ...

    @abstractmethod
    def delete(self, episode_id: str) -> EpisodeRecord:
        """Delete one record by id."""
        ...

    @abstractmethod
    def list(
        self,
        limit: int = 100,
        *,
        project_id: str | None = None,
        scope: EpisodeSearchScope | None = None,
    ) -> Sequence[EpisodeRecord]:
        """Return records ordered by created_at, then id. File mtime is not a key."""
        ...

    @abstractmethod
    def search(
        self,
        query: str = "",
        *,
        status: EpisodeStatus | None = None,
        limit: int = 20,
        scope: EpisodeSearchScope = "current_project",
        project_id: str = "",
    ) -> Sequence[EpisodeSearchHit]:
        """Rank records in scope by lexical score.

        ``lexical_score`` is a BM25 rank key, not a 0–1 similarity. An empty
        query returns recent records with score 0. Other projects appear only
        when ``scope`` is ``all_projects``.
        """
        ...

    @abstractmethod
    def recent(
        self,
        *,
        project_id: str,
        limit: int = 10,
    ) -> Sequence[EpisodeRecord]:
        """Newest records for one project, by created_at then id."""
        ...


__all__ = ["IEpisodeStore"]
