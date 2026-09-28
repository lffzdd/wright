"""Agent 线程与唯一 stdin 收集线程之间的交互邮箱。

Agent / 工具线程调用 ``request()`` 后只等待结果，不读终端。
REPL 输入线程 ``poll()`` 到请求后收集用户输入，再 ``resolve()``。
批准、拒绝、取消和关闭都走同一个终态入口：一次持久化、一次唤醒。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from ...application.session.publisher import EventPublisher

InteractionKind = Literal["permission", "ask_user"]

# prompt_toolkit Application.exit(result=...) 用来打断正在进行的主输入。
PROMPT_INTERRUPTED = object()


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


def _audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


class InteractionHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[InteractionRequest] = deque()
        self._requests: dict[str, InteractionRequest] = {}
        self._closed = False
        self._collector_ident: int | None = None
        self._interrupt: Callable[[], None] | None = None
        self._on_requested: Callable[[str, InteractionKind, dict[str, Any]], None] | None = None
        self._on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None] | None = None

    def set_persistence(
        self,
        on_requested: Callable[[str, InteractionKind, dict[str, Any]], None],
        on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None],
    ) -> None:
        with self._lock:
            self._on_requested = on_requested
            self._on_resolved = on_resolved

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
        with self._lock:
            return any(item.state == "pending" for item in self._requests.values())

    def request(self, kind: InteractionKind, payload: dict[str, Any]) -> Any:
        """Agent / 工具线程：投递请求并阻塞直到收集线程回复。"""
        item = InteractionRequest(kind=kind, payload=payload)
        with self._lock:
            if self._closed:
                return "deny" if kind == "permission" else None
            self._pending.append(item)
            self._requests[item.request_id] = item
            callback = self._on_requested
        if callback is not None:
            callback(item.request_id, kind, _audit_payload(dict(payload)))
        interrupt = self._interrupt
        if interrupt is not None:
            interrupt()
        item.done.wait()
        return item.answer

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
            item.done.set()
        return True

    def cancel_pending(self) -> None:
        self._finish_open("cancelled", wake_answer=None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel_pending()

    def _finish_open(self, status: str, *, wake_answer: Any) -> None:
        with self._lock:
            pending = [item for item in self._requests.values() if item.state == "pending"]
            for item in pending:
                item.answer = "deny" if item.kind == "permission" else wake_answer
                item.state = status
                self._requests.pop(item.request_id, None)
            self._pending.clear()
            callback = self._on_resolved
        for item in pending:
            if callback is not None:
                try:
                    callback(item.request_id, item.kind, {"status": "cancelled", "choice": "cancel"})
                except Exception:
                    pass
            item.done.set()

    def wait_for_idle_or_request(self, idle: threading.Event) -> None:
        """Agent 忙碌时不画主输入框，但仍能被权限请求唤醒。"""
        while not idle.is_set() and not self.has_pending():
            idle.wait(timeout=0.1)


class InteractionBroker:
    """Browser-safe interaction rendezvous keyed by stable request ids."""

    def __init__(self, publisher: EventPublisher) -> None:
        self.publisher = publisher
        self._lock = threading.RLock()
        self._pending: dict[str, InteractionRequest] = {}
        self._closed = False
        self._on_requested: Callable[[str, InteractionKind, dict[str, Any]], None] | None = None
        self._on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None] | None = None

    def set_persistence(
        self,
        on_requested: Callable[[str, InteractionKind, dict[str, Any]], None],
        on_resolved: Callable[[str, InteractionKind, dict[str, Any]], None],
    ) -> None:
        """Attach the durable fact owner after runtime assembly."""
        with self._lock:
            self._on_requested = on_requested
            self._on_resolved = on_resolved

    def request(self, kind: InteractionKind, payload: dict[str, Any]) -> Any:
        item = InteractionRequest(kind=kind, payload=dict(payload))
        with self._lock:
            if self._closed:
                return "deny" if kind == "permission" else None
            self._pending[item.request_id] = item
            callback = self._on_requested
        if callback is not None:
            callback(item.request_id, kind, _audit_payload(dict(payload)))
        event_payload = dict(payload)
        event_payload.update({"request_id": item.request_id, "kind": kind})
        self.publisher.publish("interaction.requested", event_payload)
        item.done.wait()
        return item.answer

    def resolve(self, request_id: str, answer: Any) -> bool:
        with self._lock:
            item = self._pending.get(request_id)
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
                return False
        with self._lock:
            if item.state != "committing":
                return False
            item.answer = answer
            item.state = str(record["status"])
            self._pending.pop(request_id, None)
            item.done.set()
        self.publisher.publish(
            "interaction.resolved",
            {"request_id": request_id, "kind": item.kind, "status": record["status"]},
        )
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**item.payload, "request_id": request_id, "kind": item.kind}
                for request_id, item in self._pending.items()
                if item.state in {"pending", "committing"}
            ]

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel_pending()

    def cancel_pending(self) -> None:
        with self._lock:
            pending = [
                (request_id, item)
                for request_id, item in self._pending.items()
                if item.state == "pending"
            ]
            for request_id, item in pending:
                item.answer = "deny" if item.kind == "permission" else None
                item.state = "cancelled"
                self._pending.pop(request_id, None)
            callback = self._on_resolved
        for request_id, item in pending:
            if callback is not None:
                try:
                    callback(request_id, item.kind, {"status": "cancelled", "choice": "cancel"})
                except Exception:
                    pass
            self.publisher.publish(
                "interaction.resolved",
                {
                    "request_id": request_id,
                    "kind": item.kind,
                    "status": "cancelled",
                    "cancelled": True,
                },
            )
            item.done.set()
