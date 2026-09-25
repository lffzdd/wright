"""Fact repository port for deterministic fact and preference persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..model.fact import Fact


class IFactRepository(ABC):
    """Port for loading, upserting, and managing factual/semantic memory."""

    @abstractmethod
    def get_facts(self, scope: str | None = None) -> Sequence[Any]:
        """Retrieve facts, optionally filtered by scope ('PROJECT' or 'GLOBAL')."""
        ...

    @abstractmethod
    def save_fact(self, fact: Any) -> None:
        """Upsert a fact (overwrites existing fact with same key/id)."""
        ...

    @abstractmethod
    def delete_fact(self, fact_id: str) -> bool:
        """Delete a fact by identifier, returning True if deleted."""
        ...


__all__ = ["IFactRepository"]
