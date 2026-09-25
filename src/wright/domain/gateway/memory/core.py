"""Core memory store gateway port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...model.memory import CoreMemory


class ICoreMemoryStore(ABC):
    """Port for loading and persisting the agent's core memory."""

    @abstractmethod
    def load(self) -> Any:
        """Load the CoreMemory entity."""
        ...

    @abstractmethod
    def save(self, core_memory: Any) -> None:
        """Persist the CoreMemory entity."""
        ...


__all__ = ["ICoreMemoryStore"]
