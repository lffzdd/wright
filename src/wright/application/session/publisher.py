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


class DisplayFold:
    """Fold of UI events used only to rebuild the current view.

    Completed conversation history stays on the session. This object keeps
    the active root turn, child-agent progress, and the latest usage sample.
    """

    def __init__(self) -> None:
        self.root: dict[str, Any] | None = None
        self.agents: dict[str, dict[str, Any]] = {}
        self.request_usage: dict[str, Any] | None = None
        self.task_usage: dict[str, Any] | None = None
        self.context_tokens: int | None = None
        self.context_limit: int | None = None
        self.published_turns: set[str] = set()
        self.published_runs: set[str] = set()
        self._seeded = False

    def apply(self, event: UiEventEnvelope) -> None:
        payload = event.payload
        depth = int(payload.get("agent_depth") or 0)
        task_id = str(payload.get("agent_task_id") or "")
        if depth > 0 or task_id:
            self._apply_agent(event, task_id or "child", depth)
            return
        if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
            if event.turn_id:
                self.published_turns.add(event.turn_id)
            run_id = str(payload.get("run_id") or "")
            if run_id:
                self.published_runs.add(run_id)
            self.root = None
            return
        if event.type == "turn.started":
            self.root = {
                "turn_id": event.turn_id,
                "run_id": str(payload.get("run_id") or ""),
                "prompt": str(payload.get("prompt") or ""),
                "attachments": list(payload.get("attachments") or []),
                "reasoning": "",
                "content": "",
                "tools": {},
            }
        elif self.root is None:
            if event.type == "usage.request":
                self._usage_request(payload)
            elif event.type == "usage.task":
                self.task_usage = {
                    "prompt_tokens": payload.get("prompt_tokens"),
                    "completion_tokens": payload.get("completion_tokens"),
                    "total_tokens": payload.get("total_tokens"),
                }
            return
        elif event.type == "reasoning.delta":
            self.root["reasoning"] += str(payload.get("piece") or "")
        elif event.type == "content.delta":
            self.root["content"] += str(payload.get("piece") or "")
        elif event.type == "content.final":
            self.root["content"] = payload.get("content")
        elif event.type.startswith("tool."):
            call_id = str(payload.get("call_id") or "")
            tool = self.root["tools"].setdefault(call_id, {"call_id": call_id})
            if event.type == "tool.output":
                tool["output"] = str(tool.get("output") or "") + str(payload.get("output") or "")
            else:
                tool.update(payload)
                tool["phase"] = (
                    "succeeded" if event.type == "tool.finished" and payload.get("ok")
                    else "failed" if event.type == "tool.finished"
                    else event.type.removeprefix("tool.")
                )
        elif event.type == "usage.request":
            self._usage_request(payload)
        elif event.type == "usage.task":
            self.task_usage = {
                "prompt_tokens": payload.get("prompt_tokens"),
                "completion_tokens": payload.get("completion_tokens"),
                "total_tokens": payload.get("total_tokens"),
            }

    def _usage_request(self, payload: dict[str, Any]) -> None:
        self.request_usage = {
            "prompt_tokens": payload.get("prompt_tokens"),
            "completion_tokens": payload.get("completion_tokens"),
            "total_tokens": payload.get("total_tokens"),
        }
        if payload.get("context_tokens") is not None:
            self.context_tokens = payload.get("context_tokens")
        if payload.get("context_limit") is not None:
            self.context_limit = payload.get("context_limit")

    def seed(self, turn_ids: set[str], run_ids: set[str]) -> None:
        """Show history that already existed before this process published."""

        if self._seeded:
            return
        self.published_turns.update(turn_ids)
        self.published_runs.update(run_ids)
        self._seeded = True

    def _apply_agent(self, event: UiEventEnvelope, task_id: str, depth: int) -> None:
        agent = self.agents.setdefault(task_id, {
            "task_id": task_id,
            "depth": depth,
            "content": "",
            "tools": {},
            "status": "running",
        })
        payload = event.payload
        if event.type == "content.delta":
            agent["content"] += str(payload.get("piece") or "")
        elif event.type == "content.final":
            agent["content"] = payload.get("content")
            agent["status"] = "finished"
        elif event.type.startswith("tool."):
            call_id = str(payload.get("call_id") or "")
            tool = agent["tools"].setdefault(call_id, {"call_id": call_id})
            if event.type != "tool.output":
                tool.update(payload)
            else:
                tool["output"] = str(tool.get("output") or "") + str(payload.get("output") or "")
        elif event.type == "usage.request":
            agent["request_usage"] = {
                "prompt_tokens": payload.get("prompt_tokens"),
                "completion_tokens": payload.get("completion_tokens"),
                "total_tokens": payload.get("total_tokens"),
            }

    def view(self) -> dict[str, Any]:
        root = None
        if self.root is not None:
            root = {**self.root, "tools": list(self.root["tools"].values())}
        agents = []
        for agent in self.agents.values():
            agents.append({**agent, "tools": list(agent["tools"].values())})
        return {
            "active_turn": root,
            "agents": agents,
            "request_usage": self.request_usage,
            "task_usage": self.task_usage,
            "context_tokens": self.context_tokens,
            "context_limit": self.context_limit,
        }


def _drain(target: queue.Queue) -> None:
    while True:
        try:
            target.get_nowait()
        except queue.Empty:
            return


class EventPublisher:
    """Order, retain and fan out one runtime's event stream."""

    def __init__(
        self,
        *,
        project_id: str,
        session_id: str,
        max_events: int = 2_000,
        subscriber_queue_max: int = 256,
    ) -> None:
        if max_events <= 0 or subscriber_queue_max <= 0:
            raise ValueError("event buffers must be positive")
        self.project_id = project_id
        self.session_id = session_id
        self.stream_id = uuid4().hex
        self.max_events = max_events
        self._seq = 0
        self._events: deque[UiEventEnvelope] = deque(maxlen=max_events)
        self._subscribers: dict[str, queue.Queue[UiEventEnvelope]] = {}
        self._listeners: dict[str, Callable[[UiEventEnvelope], None]] = {}
        self._stale: set[str] = set()
        self.subscriber_queue_max = subscriber_queue_max
        self.display = DisplayFold()
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
            self.display.apply(event)
            listeners = tuple(self._listeners.values())
            overflow: list[str] = []
            for subscriber_id, target in self._subscribers.items():
                try:
                    target.put_nowait(event)
                except queue.Full:
                    overflow.append(subscriber_id)
            for subscriber_id in overflow:
                self._stale.add(subscriber_id)
                stale_queue = self._subscribers.get(subscriber_id)
                if stale_queue is not None:
                    _drain(stale_queue)
        for listener in listeners:
            listener(event)
        return event

    def capture(self, build: Callable[[int, str], Any]) -> Any:
        """Build a snapshot at the current seq while publish cannot interleave.

        The caller must not do network I/O inside ``build``. The returned
        value's sequence watermark is the seq observed in this critical
        section, not a later read.
        """

        with self._lock:
            return build(self._seq, self.stream_id)

    def take_stale(self, subscriber_id: str) -> bool:
        """Report and clear a subscriber whose queue overflowed."""

        with self._lock:
            if subscriber_id not in self._stale:
                return False
            self._stale.discard(subscriber_id)
            return True

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
        target: queue.Queue[UiEventEnvelope] = queue.Queue(maxsize=self.subscriber_queue_max)
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
            self._stale.discard(subscriber_id)

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
