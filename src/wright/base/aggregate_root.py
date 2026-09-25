"""Aggregate Root abstraction for DDD aggregates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base_entity import BaseEntity


@dataclass(eq=False)
class AggregateRoot(BaseEntity):
    """Aggregate Root supporting recording and draining domain events."""

    _domain_events: list[Any] = field(default_factory=list, init=False, repr=False)

    def record_event(self, event: Any) -> None:
        """Record a domain event to be dispatched when changes are committed."""
        self._domain_events.append(event)
        self.mark_updated()

    def pull_events(self) -> list[Any]:
        """Drain and return all recorded domain events, clearing internal buffer."""
        events = list(self._domain_events)
        self._domain_events.clear()
        return events

    def clear_events(self) -> None:
        """Clear all pending domain events."""
        self._domain_events.clear()


__all__ = ["AggregateRoot"]
