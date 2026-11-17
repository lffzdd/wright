"""One store for every kind of memory record.

Spaces, facts, episodes, commitments, and the change log share one database so
a consolidation pass can change several records atomically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from ...model.memory import ChangeRecord, Commitment, EpisodeRecord, Fact, Space


class IMemoryStore(ABC):
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes. Nested use joins the outer transaction."""
        yield

    # spaces
    @abstractmethod
    def upsert_space(self, space: Space) -> Space: ...

    @abstractmethod
    def get_space(self, space_id: str) -> Space | None: ...

    @abstractmethod
    def list_spaces(self) -> list[Space]: ...

    # facts
    @abstractmethod
    def insert_fact(self, fact: Fact) -> Fact: ...

    @abstractmethod
    def replace_fact(self, fact: Fact, *, expected_revision: int | None = None) -> Fact:
        """Write ``fact`` over the stored row. Raises MemoryConflictError on a stale revision."""

    @abstractmethod
    def get_fact(self, fact_id: str) -> Fact | None: ...

    @abstractmethod
    def list_facts(
        self,
        *,
        space_ids: Sequence[str] | None = None,
        statuses: Sequence[str] | None = ("active",),
        tiers: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[Fact]: ...

    @abstractmethod
    def touch_facts(self, fact_ids: Sequence[str], at: str) -> None:
        """Record a use without bumping the revision."""

    @abstractmethod
    def purge_fact(self, fact_id: str) -> None: ...

    # episodes
    @abstractmethod
    def save_episode(self, episode: EpisodeRecord) -> EpisodeRecord:
        """Insert once. Saving the same id again keeps the first record."""

    @abstractmethod
    def replace_episode(self, episode: EpisodeRecord) -> EpisodeRecord: ...

    @abstractmethod
    def get_episode(self, episode_id: str) -> EpisodeRecord | None: ...

    @abstractmethod
    def list_episodes(
        self, *, project_ids: Sequence[str] | None = None, limit: int | None = None
    ) -> list[EpisodeRecord]:
        """Newest first."""

    @abstractmethod
    def delete_episode(self, episode_id: str) -> bool: ...

    # commitments
    @abstractmethod
    def save_commitment(self, item: Commitment) -> Commitment: ...

    @abstractmethod
    def get_commitment(self, commitment_id: str) -> Commitment | None: ...

    @abstractmethod
    def list_commitments(
        self, *, space_ids: Sequence[str] | None = None, statuses: Sequence[str] | None = None
    ) -> list[Commitment]: ...

    @abstractmethod
    def delete_commitment(self, commitment_id: str) -> bool: ...

    # change log
    @abstractmethod
    def append_change(
        self,
        *,
        at: str,
        actor: str,
        action: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        note: str = "",
    ) -> ChangeRecord: ...

    @abstractmethod
    def list_changes(self, *, limit: int = 100, target_id: str = "") -> list[ChangeRecord]: ...

    @abstractmethod
    def get_change(self, change_id: int) -> ChangeRecord | None: ...

    @abstractmethod
    def mark_reverted(self, change_id: int) -> None: ...

    # small key/value state, for example the last consolidation time
    @abstractmethod
    def get_meta(self, key: str) -> str | None: ...

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None: ...


__all__ = ["IMemoryStore"]
