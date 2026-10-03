"""Adapters between the shared interaction mailbox and a host.

``InteractionHub`` wakes the one terminal collector. ``InteractionBroker``
publishes the same requests to a browser. Neither owns a second state machine.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..application.session.mailbox import (
    InteractionKind,
    InteractionMailbox,
    InteractionRequest,
    decision_record,
)

if TYPE_CHECKING:
    from ..application.session.publisher import EventPublisher

# prompt_toolkit Application.exit(result=...) 用来打断正在进行的主输入。
PROMPT_INTERRUPTED = object()


class InteractionHub:
    """Terminal adapter: poll on the collector thread, interrupt its prompt."""

    def __init__(self) -> None:
        self._mailbox = InteractionMailbox(delivers=True)
        self._collector_ident: int | None = None
        self._interrupt: Callable[[], None] | None = None

    def set_persistence(
        self,
        on_requested: Callable[[str, InteractionKind, dict[str, Any]], None],
        on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None],
    ) -> None:
        self._mailbox.set_persistence(on_requested, on_resolved)

    def bind_collector(self, interrupt: Callable[[], None] | None = None) -> None:
        """在唯一读 stdin 的线程里调用一次。"""
        self._collector_ident = threading.get_ident()
        self._interrupt = interrupt

    def is_collector_thread(self) -> bool:
        return (
            self._collector_ident is not None
            and threading.get_ident() == self._collector_ident
        )

    def has_pending(self) -> bool:
        return self._mailbox.has_pending()

    @property
    def closed(self) -> bool:
        return self._mailbox.closed

    def request(self, kind: InteractionKind, payload: dict[str, Any], *, validator=None) -> Any:
        item = self._mailbox.begin(kind, payload)
        if item is None:
            return "deny" if kind == "permission" else None
        interrupt = self._interrupt
        if interrupt is not None:
            interrupt()
        return self._mailbox.wait(item, validator=validator, resolve=self.resolve)

    def poll(self) -> InteractionRequest | None:
        return self._mailbox.poll()

    def snapshot(self) -> list[dict[str, Any]]:
        return self._mailbox.snapshot()

    def resolve(self, request_id: str, answer: Any) -> bool:
        return self._mailbox.resolve(request_id, answer)

    def cancel_pending(self) -> None:
        self._mailbox.cancel_pending()

    def close(self) -> None:
        self._mailbox.close()

    def wait_for_idle_or_request(self, idle: threading.Event) -> None:
        """Agent 忙碌时不画主输入框，但仍能被权限请求唤醒。"""
        while not idle.is_set() and not self.has_pending():
            idle.wait(timeout=0.1)


class InteractionBroker:
    """Browser adapter: the mailbox blocks, and this object publishes events."""

    def __init__(self, publisher: EventPublisher) -> None:
        self.publisher = publisher
        self._mailbox = InteractionMailbox(delivers=True)

    def set_persistence(
        self,
        on_requested: Callable[[str, InteractionKind, dict[str, Any]], None],
        on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None],
    ) -> None:
        self._mailbox.set_persistence(on_requested, on_resolved)

    def request(self, kind: InteractionKind, payload: dict[str, Any], *, validator=None) -> Any:
        item = self._mailbox.begin(kind, payload)
        if item is None:
            return "deny" if kind == "permission" else None
        event_payload = dict(payload)
        event_payload.update({"request_id": item.request_id, "kind": kind})
        self.publisher.publish("interaction.requested", event_payload)
        answer = self._mailbox.wait(item, validator=validator, resolve=self.resolve)
        return answer

    def resolve(self, request_id: str, answer: Any) -> bool:
        pending = {
            entry["request_id"]: entry for entry in self._mailbox.snapshot()
        }
        current = pending.get(request_id, {})
        kind = str(current.get("kind") or "permission")
        resolved = self._mailbox.resolve(request_id, answer)
        if not resolved:
            return False
        record = decision_record(kind, answer, current)  # type: ignore[arg-type]
        self.publisher.publish(
            "interaction.resolved",
            {"request_id": request_id, "kind": kind, "status": record["status"]},
        )
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        return self._mailbox.snapshot()

    @property
    def closed(self) -> bool:
        return self._mailbox.closed

    def close(self) -> None:
        self._mailbox.close(before_wake=self._publish_cancelled)

    def cancel_pending(self) -> None:
        self._mailbox.cancel_pending(before_wake=self._publish_cancelled)

    def _publish_cancelled(self, item: InteractionRequest) -> None:
        self.publisher.publish(
            "interaction.resolved",
            {
                "request_id": item.request_id,
                "kind": item.kind,
                "status": "cancelled",
                "cancelled": True,
            },
        )


__all__ = [
    "PROMPT_INTERRUPTED",
    "InteractionBroker",
    "InteractionHub",
    "InteractionKind",
    "InteractionRequest",
    "decision_record",
]
