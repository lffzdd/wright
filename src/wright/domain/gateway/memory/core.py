"""Core memory store gateway port.

The port is scoped. A global update cannot persist a project anchor, and a
project update cannot persist the global profile. Callers compose a view
after the independent reads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...model.memory.core import GlobalCoreRecord, ProjectCoreRecord


class ICoreMemoryStore(ABC):
    """Port for the global core record and per-project anchors."""

    @abstractmethod
    def load_global(self) -> GlobalCoreRecord:
        """Load persona and the global human profile."""

    @abstractmethod
    def update_global(self, mutate: Callable[[GlobalCoreRecord], None]) -> GlobalCoreRecord:
        """Read, mutate, and save the global record.

        The callback must not perform I/O or re-enter the store. The
        implementation keeps the loaded persona, so this path cannot change it.
        """

    @abstractmethod
    def load_project(self, project_id: str) -> ProjectCoreRecord:
        """Load one project's anchor. A missing file is an empty anchor."""

    @abstractmethod
    def update_project(
        self, project_id: str, mutate: Callable[[ProjectCoreRecord], None],
    ) -> ProjectCoreRecord:
        """Read, mutate, and save one project's anchor.

        The callback must not perform I/O or re-enter the store. Other
        projects and the global record are outside this lock.
        """


__all__ = ["ICoreMemoryStore"]
