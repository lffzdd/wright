"""Core memory store gateway port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...model.memory import CoreMemory


class ICoreMemoryStore(ABC):
    """Port for loading and persisting the agent's core memory."""

    @abstractmethod
    def load(self) -> CoreMemory:
        """Load the CoreMemory entity."""
        ...

    @abstractmethod
    def save(self, core_memory: CoreMemory) -> None:
        """Persist the CoreMemory entity."""
        ...

    @abstractmethod
    def update(self, mutate: Callable[[CoreMemory], None]) -> CoreMemory:
        """Read, mutate and save atomically, returning the saved entity.

        The callback only changes the supplied entity; it must not perform I/O
        or re-enter the store. If it raises, nothing is persisted.
        Implementations serialize updates across clients of the same store.
        """
        ...


__all__ = ["ICoreMemoryStore"]
