"""Base domain entity abstractions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(eq=False)
class BaseEntity:
    """Domain entity with identity and timestamps."""

    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def mark_updated(self) -> None:
        """Update timestamp to current time."""
        self.updated_at = time.time()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, self.__class__):
            return False
        return self.id == other.id

    def __hash__(self) -> int:
        return hash((self.__class__, self.id))
