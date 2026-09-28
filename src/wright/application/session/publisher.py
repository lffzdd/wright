"""Publish, retain, and fan out one session's event stream."""

from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ...domain.model.session import Session
from .events import (
    UI_EVENT_TYPES,
    UI_EVENT_VERSION,
    EventScope,
    SessionEvents,
    UiEventEnvelope,
    _json_value,
)
from .live_resources import RuntimeResources


class EventPublisher:
    """Order, retain and fan out one runtime's event stream."""

    def __init__(self, *, project_id: str, session_id: str, max_events: int = 2_000) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.project_id = project_id
        self.session_id = session_id
        self.stream_id = uuid4().hex
        self.max_events = max_events
        self._seq = 0
        self._events: deque[UiEventEnvelope] = deque(maxlen=max_events)
        self._subscribers: dict[str, queue.Queue[UiEventEnvelope]] = {}
        self._listeners: dict[str, Callable[[UiEventEnvelope], None]] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        turn_id: str | None = None,
    ) -> UiEventEnvelope:
        if event_type not in UI_EVENT_TYPES:
            raise ValueError(f"unknown UI event type: {event_type}")
        with self._lock:
            if self._closed:
                raise RuntimeError("event publisher is closed")
            self._seq += 1
            event = UiEventEnvelope(
                version=UI_EVENT_VERSION,
                stream_id=self.stream_id,
                event_id=uuid4().hex,
                seq=self._seq,
                emitted_at=datetime.now(timezone.utc).isoformat(),
                project_id=self.project_id,
                session_id=self.session_id,
                turn_id=turn_id,
                type=event_type,
                payload=_json_value(payload or {}),
            )
            self._events.append(event)
            queues = tuple(self._subscribers.values())
            listeners = tuple(self._listeners.values())
        for target in queues:
            target.put(event)
        for listener in listeners:
            listener(event)
        return event

    def replay(
        self, stream_id: str | None, last_seq: int | None,
    ) -> list[UiEventEnvelope] | None:
        """Return missed events, or None when an authoritative snapshot is needed."""
        with self._lock:
            if stream_id is not None and stream_id != self.stream_id:
                return None
            if last_seq is None:
                return []
            if last_seq < 0 or last_seq > self._seq:
                return None
            if not self._events:
                return [] if last_seq == self._seq else None
            oldest = self._events[0].seq
            if last_seq < oldest - 1:
                return None
            return [event for event in self._events if event.seq > last_seq]

    def subscribe(self) -> tuple[str, queue.Queue[UiEventEnvelope]]:
        subscriber_id = uuid4().hex
        target: queue.Queue[UiEventEnvelope] = queue.Queue()
        with self._lock:
            if self._closed:
                raise RuntimeError("event publisher is closed")
            self._subscribers[subscriber_id] = target
        return subscriber_id, target

    def retained_events(self) -> list[UiEventEnvelope]:
        with self._lock:
            return list(self._events)

    def unsubscribe(self, subscriber_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def add_listener(self, listener: Callable[[UiEventEnvelope], None]) -> str:
        listener_id = uuid4().hex
        with self._lock:
            self._listeners[listener_id] = listener
        return listener_id

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()
            self._listeners.clear()


def open_session_events(
    session: Session,
    *,
    project_id: str = "local",
    publisher: EventPublisher | None = None,
    scope: EventScope | None = None,
    runtime_resources: RuntimeResources | None = None,
) -> SessionEvents:
    """Open an application event channel. No renderer is required."""
    bus = publisher or EventPublisher(
        project_id=project_id,
        session_id=session.session_id,
    )
    return SessionEvents(bus, runtime_resources=runtime_resources, scope=scope)
