"""Domain gateways (Ports) defining dependency inversion contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any, Optional


class ILLMProvider(ABC):
    """Port for Large Language Model completions and streaming."""

    @abstractmethod
    def __call__(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Generate completion events for given messages and tools."""
        ...


class IToolExecutor(ABC):
    """Port for executing tool calls produced by the agent."""

    @abstractmethod
    def execute(self, calls: Sequence[Any]) -> Sequence[Any]:
        """Execute a batch of tool calls and return outcomes."""
        ...


class ISessionRepository(ABC):
    """Port for persisting and retrieving session states."""

    @abstractmethod
    def save(self, session: Any) -> None:
        """Persist session state."""
        ...

    @abstractmethod
    def load(self, session_id: str) -> Optional[Any]:
        """Load session state by identifier."""
        ...


class IMemoryStore(ABC):
    """Port for episodic or semantic memory recall and persistence."""

    @abstractmethod
    def recall(self, query: str, limit: int = 5) -> Sequence[Any]:
        """Recall relevant memory items."""
        ...

    @abstractmethod
    def record(self, item: Any) -> None:
        """Persist a memory episode or fact."""
        ...


from .task_backend import TaskBackend

__all__ = [
    "ILLMProvider",
    "IToolExecutor",
    "ISessionRepository",
    "IMemoryStore",
    "TaskBackend",
]
