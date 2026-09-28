"""Single interaction lifecycle for permission and ask_user requests.

Terminal wakeup and web delivery are adapters. They share this state machine:
one persistence attempt, one wake, and fail-closed when nobody can answer.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

InteractionKind = Literal["permission", "ask_user"]


def decision_record(kind: InteractionKind, answer: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Audit the decision without storing free-text answers or secret bodies."""

    if kind == "permission":
        choice = "deny" if answer is None else str(answer)
        status = "approved" if choice.startswith("allow") else "denied"
        return {
            "status": status,
            "choice": choice,
            "tool_name": str(payload.get("tool_name", "")),
        }
    if answer is None:
        return {"status": "cancelled", "choice": "cancel"}
    return {"status": "approved", "choice": "answered"}


def audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    hidden = {"preview", "headers", "body", "authorization"}
    return {key: value for key, value in payload.items() if key not in hidden}


@dataclass
class InteractionRequest:
    kind: InteractionKind
    payload: dict[str, Any]
    request_id: str = field(default_factory=lambda: uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    answer: Any = None
    state: str = "pending"


class InteractionMailbox:
    """Own pending requests, durable commit, cancellation, and close."""

    def __init__(self, *, delivers: bool = True) -> None:
        self.delivers = delivers
        self._lock = threading.RLock()
        self._pending: deque[InteractionRequest] = deque()
        self._requests: dict[str, InteractionRequest] = {}
        self._closed = False
        self._on_requested: Callable[[str, InteractionKind, dict[str, Any]], None] | None = None
        self._on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None] | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def set_persistence(
        self,
        on_requested: Callable[[str, InteractionKind, dict[str, Any]], None],
        on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None],
    ) -> None:
        with self._lock:
            self._on_requested = on_requested
            self._on_resolved = on_resolved

    def has_pending(self) -> bool:
        with self._lock:
            return any(item.state == "pending" for item in self._requests.values())

    def begin(self, kind: InteractionKind, payload: dict[str, Any]) -> InteractionRequest | None:
        """Register a request. None means fail closed without waiting."""

        item = InteractionRequest(kind=kind, payload=dict(payload))
        with self._lock:
            if self._closed or not self.delivers:
                return None
            self._pending.append(item)
            self._requests[item.request_id] = item
            callback = self._on_requested
        if callback is not None:
            try:
                callback(item.request_id, kind, audit_payload(dict(payload)))
            except Exception:
                with self._lock:
                    self._requests.pop(item.request_id, None)
                    self._pending = deque(
                        pending for pending in self._pending
                        if pending.request_id != item.request_id
                    )
                raise
        return item

    def wait(self, item: InteractionRequest) -> Any:
        item.done.wait()
        return item.answer

    def request(self, kind: InteractionKind, payload: dict[str, Any]) -> Any:
        item = self.begin(kind, payload)
        if item is None:
            return "deny" if kind == "permission" else None
        return self.wait(item)

    def poll(self) -> InteractionRequest | None:
        with self._lock:
            while self._pending:
                item = self._pending.popleft()
                if item.request_id in self._requests and item.state == "pending":
                    return item
            return None

    def resolve(self, request_id: str, answer: Any) -> bool:
        with self._lock:
            item = self._requests.get(request_id)
            if item is None or item.state != "pending":
                return False
            item.state = "committing"
            callback = self._on_resolved
        record = decision_record(item.kind, answer, item.payload)
        if callback is not None:
            try:
                callback(request_id, item.kind, record)
            except Exception:
                with self._lock:
                    if item.state == "committing":
                        item.state = "pending"
                        if item not in self._pending:
                            self._pending.append(item)
                return False
        with self._lock:
            if item.state != "committing":
                return False
            item.answer = answer
            item.state = str(record["status"])
            self._requests.pop(request_id, None)
            self._pending = deque(
                pending for pending in self._pending if pending.request_id != request_id
            )
            item.done.set()
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**item.payload, "request_id": request_id, "kind": item.kind}
                for request_id, item in self._requests.items()
                if item.state in {"pending", "committing"}
            ]

    def cancel_pending(
        self,
        *,
        before_wake: Callable[[InteractionRequest], None] | None = None,
    ) -> list[InteractionRequest]:
        with self._lock:
            pending = [item for item in self._requests.values() if item.state == "pending"]
            for item in pending:
                item.answer = "deny" if item.kind == "permission" else None
                item.state = "cancelled"
                self._requests.pop(item.request_id, None)
            self._pending.clear()
            callback = self._on_resolved
        for item in pending:
            if callback is not None:
                try:
                    callback(
                        item.request_id, item.kind, {"status": "cancelled", "choice": "cancel"},
                    )
                except Exception:
                    pass
            if before_wake is not None:
                before_wake(item)
            item.done.set()
        return pending

    def close(self, *, before_wake: Callable[[InteractionRequest], None] | None = None) -> None:
        with self._lock:
            self._closed = True
        self.cancel_pending(before_wake=before_wake)


__all__ = [
    "InteractionKind",
    "InteractionMailbox",
    "InteractionRequest",
    "audit_payload",
    "decision_record",
]
