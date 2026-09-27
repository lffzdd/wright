"""Semantic memory store port: durable facts, not episode traces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ...model.memory import SemanticMemoryRecord, SemanticMemoryType


class ISemanticMemoryStore(ABC):
    """Port for the markdown semantic memories the application persists.

    ``create`` never updates an existing id. ``update`` never creates one.
    List and search filter by scope and status before they apply ``limit``.
    """

    @abstractmethod
    def list(
        self,
        limit: int = 100,
        *,
        read_scope: str = "applicable",
        project_id: str = "",
        include_inactive: bool = False,
        type_: SemanticMemoryType | None = None,
    ) -> Sequence[SemanticMemoryRecord]:
        """Return memories in the requested scope, newest first, up to limit."""
        ...

    @abstractmethod
    def get(self, memory_id: str) -> SemanticMemoryRecord:
        """Load one semantic memory by its stable id. Inactive files are readable."""
        ...

    @abstractmethod
    def search(
        self,
        query: str = "",
        *,
        limit: int = 20,
        read_scope: str = "applicable",
        project_id: str = "",
        include_inactive: bool = False,
        type_: SemanticMemoryType | None = None,
    ) -> Sequence[SemanticMemoryRecord]:
        """Return memories matching query inside the requested scope."""
        ...

    @abstractmethod
    def create(
        self,
        *,
        name: str,
        description: str,
        type_: SemanticMemoryType,
        content: str,
        scope: str,
        project_id: str = "",
        origin: str = "",
        source_refs: tuple[str, ...] = (),
        memory_id: str = "",
    ) -> SemanticMemoryRecord:
        """Create one memory. A matching display name does not overwrite."""
        ...

    @abstractmethod
    def update(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        read_scope: str = "applicable",
        project_id: str = "",
        name: str | None = None,
        description: str | None = None,
        type_: SemanticMemoryType | None = None,
        content: str | None = None,
        status: str | None = None,
        origin: str | None = None,
        source_refs: tuple[str, ...] | None = None,
        provenance_set: bool = False,
    ) -> SemanticMemoryRecord:
        """Update one id. Stale ``expected_revision`` is a conflict, not a retry."""
        ...

    @abstractmethod
    def delete(
        self,
        memory_id: str,
        *,
        read_scope: str = "applicable",
        project_id: str = "",
    ) -> SemanticMemoryRecord:
        """Delete one memory that is inside the requested scope."""
        ...

    def read_index(self) -> str:
        """Human navigation file. Agent recall must not treat this as the source of truth."""
        return ""


__all__ = ["ISemanticMemoryStore"]
