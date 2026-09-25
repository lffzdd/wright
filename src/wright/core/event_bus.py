"""In-memory thread-safe EventBus for decoupling UI, application, and background tasks."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any


class EventBus:
    """Publish-subscribe event bus supporting typed topics and synchronous/queued handlers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        """Subscribe a handler callback to a topic.

        Returns an unsubscribe callable.
        """
        with self._lock:
            self._subscribers[topic].append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                if handler in self._subscribers[topic]:
                    self._subscribers[topic].remove(handler)

        return _unsubscribe

    def publish(self, topic: str, data: Any = None) -> None:
        """Publish an event to all subscribers of a topic."""
        with self._lock:
            handlers = list(self._subscribers.get(topic, []))
            all_handlers = list(self._subscribers.get("*", []))

        for handler in handlers + all_handlers:
            try:
                handler(data)
            except Exception:
                # Handlers must not break publishing chain
                pass

    def clear(self) -> None:
        """Remove all subscriptions."""
        with self._lock:
            self._subscribers.clear()


# Global default bus instance for convenience
global_bus = EventBus()
