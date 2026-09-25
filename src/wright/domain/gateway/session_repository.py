"""Session repository port definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class SessionRepository(ABC):
    """Port for persisting and retrieving session states."""

    @abstractmethod
    def save(self, session: Any) -> None:
        """Persist session state."""
        ...

    @abstractmethod
    def load(self, session_id: str) -> Optional[Any]:
        """Load session state by identifier."""
        ...


# Interface compatibility alias
ISessionRepository = SessionRepository
IStorageGateway = SessionRepository

__all__ = ["ISessionRepository", "IStorageGateway", "SessionRepository"]
