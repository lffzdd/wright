"""Agent 线程与唯一 stdin 收集线程之间的交互邮箱。

Agent / 工具线程调用 ``request()`` 后只等待结果，不读终端。
REPL 输入线程 ``poll()`` 到请求后收集用户输入，再 ``reply``。
这样权限和 ask_user 不再占用 Agent 线程的 stdin，后续全屏 UI 只需换收集端。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Queue
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from .ui_events import EventPublisher

InteractionKind = Literal["permission", "ask_user"]

# prompt_toolkit Application.exit(result=...) 用来打断正在进行的主输入。
PROMPT_INTERRUPTED = object()


@dataclass
class InteractionRequest:
    kind: InteractionKind
    payload: dict[str, Any]
    reply: Queue = field(default_factory=lambda: Queue(maxsize=1))


class InteractionHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[InteractionRequest] = deque()
        self._collector_ident: int | None = None
        self._interrupt: Callable[[], None] | None = None

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
            return bool(self._pending)

    def request(self, kind: InteractionKind, payload: dict[str, Any]) -> Any:
        """Agent / 工具线程：投递请求并阻塞直到收集线程回复。"""
        item = InteractionRequest(kind=kind, payload=payload)
        with self._lock:
            self._pending.append(item)
        interrupt = self._interrupt
        if interrupt is not None:
            interrupt()
        return item.reply.get()

    def poll(self) -> InteractionRequest | None:
        with self._lock:
            if not self._pending:
                return None
            return self._pending.popleft()

    def wait_for_idle_or_request(self, idle: threading.Event) -> None:
        """Agent 忙碌时不画主输入框，但仍能被权限请求唤醒。"""
        while not idle.is_set() and not self.has_pending():
            idle.wait(timeout=0.1)


@dataclass
class _BrokerRequest:
    kind: InteractionKind
    payload: dict[str, Any]
    reply: Queue = field(default_factory=lambda: Queue(maxsize=1))


class InteractionBroker:
    """Browser-safe interaction rendezvous keyed by stable request ids."""

    def __init__(self, publisher: EventPublisher) -> None:
        self.publisher = publisher
        self._lock = threading.RLock()
        self._pending: dict[str, _BrokerRequest] = {}
        self._closed = False

    def request(self, kind: InteractionKind, payload: dict[str, Any]) -> Any:
        request_id = uuid4().hex
        item = _BrokerRequest(kind=kind, payload=dict(payload))
        with self._lock:
            if self._closed:
                return "n" if kind == "permission" else None
            self._pending[request_id] = item
        self.publisher.publish(
            "interaction.requested",
            {"request_id": request_id, "kind": kind, **payload},
        )
        answer = item.reply.get()
        with self._lock:
            self._pending.pop(request_id, None)
        return answer

    def resolve(self, request_id: str, answer: Any) -> bool:
        with self._lock:
            item = self._pending.get(request_id)
            if item is None or item.reply.full():
                return False
            item.reply.put_nowait(answer)
        self.publisher.publish(
            "interaction.resolved",
            {"request_id": request_id, "kind": item.kind},
        )
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"request_id": request_id, "kind": item.kind, **item.payload}
                for request_id, item in self._pending.items()
            ]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.reply.put_nowait("n" if item.kind == "permission" else None)

    def cancel_pending(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.reply.put_nowait("n" if item.kind == "permission" else None)
