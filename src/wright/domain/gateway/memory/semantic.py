"""Semantic memory store port: durable facts, not episode traces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ...model.memory import SemanticMemoryRecord, SemanticMemoryType


class ISemanticMemoryStore(ABC):
    """Port for the markdown semantic memories the application persists."""

    @abstractmethod
    def list(self, limit: int = 100) -> Sequence[SemanticMemoryRecord]:
        """Return semantic memories, newest-scan order, up to limit."""
        ...

    @abstractmethod
    def get(self, memory_id: str) -> SemanticMemoryRecord:
        """Load one semantic memory by id."""
        ...

    @abstractmethod
    def search(self, query: str = "", *, limit: int = 20) -> Sequence[SemanticMemoryRecord]:
        """Return semantic memories matching query."""
        ...

    @abstractmethod
    def save(
        self,
        *,
        name: str,
        description: str,
        type_: SemanticMemoryType,
        content: str,
        origin: str = "",
        source_refs: tuple[str, ...] = (),
        memory_id: str = "",
    ) -> SemanticMemoryRecord:
        """Create a semantic memory, or update it when the id already exists."""
        ...

    @abstractmethod
    def delete(self, memory_id: str) -> SemanticMemoryRecord:
        """Delete one semantic memory and return the removed record."""
        ...

    def read_index(self) -> str:
        """Return the semantic index text, or empty when none is readable."""
        return ""


__all__ = ["ISemanticMemoryStore"]
