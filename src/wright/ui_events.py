"""Serializable UI events shared by terminal and browser frontends."""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .renderer import Renderer
from .tools.base import ToolCall, ToolResult

UI_EVENT_VERSION = 1
UI_EVENT_TYPES = frozenset({
    "session.snapshot", "session.status_changed", "turn.started",
    "turn.completed", "turn.failed", "turn.cancelled", "reasoning.delta",
    "content.delta", "content.final", "tool.planned", "tool.awaiting_approval",
    "tool.running", "tool.output", "tool.finished", "interaction.requested", "interaction.resolved",
    "task.updated", "usage.request", "usage.task", "system.notice",
    "system.checkpoint_error", "command.accepted", "command.rejected",
})


def _json_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=repr))
    except (TypeError, ValueError):
        return repr(value)


@dataclass(frozen=True)
class UiEventEnvelope:
    version: int
    stream_id: str
    event_id: str
    seq: int
    emitted_at: str
    project_id: str
    session_id: str
    type: str
    payload: dict[str, Any]
    turn_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> UiEventEnvelope:
        if value.get("version") != UI_EVENT_VERSION:
            raise ValueError(f"unsupported UI event version: {value.get('version')}")
        event_type = value.get("type")
        if event_type not in UI_EVENT_TYPES:
            raise ValueError(f"unknown UI event type: {event_type}")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("UI event payload must be an object")
        return cls(
            version=UI_EVENT_VERSION,
            stream_id=str(value["stream_id"]),
            event_id=str(value["event_id"]),
            seq=int(value["seq"]),
            emitted_at=str(value["emitted_at"]),
            project_id=str(value["project_id"]),
            session_id=str(value["session_id"]),
            turn_id=(str(value["turn_id"]) if value.get("turn_id") else None),
            type=event_type,
            payload=payload,
        )


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


class RendererEventSubscriber:
    """Project serializable UI events back into an existing terminal renderer."""

    def __init__(self, renderer: Renderer) -> None:
        self.renderer = renderer
        self._calls: dict[str, ToolCall] = {}

    def __call__(self, event: UiEventEnvelope) -> None:
        payload = event.payload
        if event.type == "reasoning.delta":
            self.renderer.on_reasoning_delta(str(payload.get("piece", "")))
        elif event.type == "content.delta":
            self.renderer.on_content_delta(str(payload.get("piece", "")))
        elif event.type == "content.final":
            self.renderer.on_final(payload.get("content"))
        elif event.type in {"tool.planned", "tool.awaiting_approval", "tool.running"}:
            call = ToolCall(
                name=str(payload.get("name", "tool")),
                arguments=dict(payload.get("arguments") or {}),
                id=str(payload.get("call_id", "")),
            )
            self._calls[call.id] = call
            if event.type == "tool.planned":
                self.renderer.on_tool_call(call)
            else:
                self.renderer.on_tool_phase(call, event.type.removeprefix("tool."))
        elif event.type == "tool.output":
            self.renderer.on_tool_output(
                str(payload.get("call_id", "")),
                str(payload.get("output", "")),
            )
        elif event.type == "tool.finished":
            call_id = str(payload.get("call_id", ""))
            call = self._calls.get(call_id) or ToolCall(
                name=str(payload.get("name", "tool")), arguments={}, id=call_id
            )
            result = ToolResult(
                ok=bool(payload.get("ok")),
                err=str(payload.get("err", "")),
                data=payload.get("data"),
            )
            self.renderer.on_tool_result(call, result)
        elif event.type == "usage.request":
            self.renderer.on_usage(
                payload.get("prompt_tokens"), payload.get("completion_tokens"),
                payload.get("total_tokens"), payload.get("context_limit"),
            )
        elif event.type == "usage.task":
            self.renderer.on_usage_summary(
                int(payload.get("prompt_tokens", 0)),
                int(payload.get("completion_tokens", 0)),
                int(payload.get("total_tokens", 0)),
            )
        elif event.type == "system.notice":
            kind = payload.get("kind")
            if kind == "completion_rejected":
                self.renderer.on_completion_rejected(payload.get("issues", ()))
            elif kind == "context_compact":
                self.renderer.on_context_compact(
                    int(payload.get("folded_count", 0)),
                    payload.get("prompt_tokens"),
                    payload.get("context_limit"),
                    float(payload.get("context_watermark", 0)),
                )
            else:
                self.renderer.on_system_notice(str(payload.get("text", "")))
        elif event.type == "system.checkpoint_error":
            self.renderer.on_checkpoint_error(str(payload.get("error", "")))
        elif event.type == "task.updated":
            self.renderer.on_agent_event(dict(payload))
        elif event.type == "session.status_changed":
            if payload.get("status") == "model_turn_started":
                self.renderer.on_turn_begin()


class PublishingRenderer(Renderer):
    """Renderer implementation that turns Agent callbacks into UI events."""

    def __init__(
        self,
        publisher: EventPublisher,
        *,
        interaction: Any = None,
        direct_renderer: Renderer | None = None,
    ) -> None:
        self.publisher = publisher
        self.interaction = interaction
        self.direct_renderer = direct_renderer
        self._active_call_id: str | None = None
        if direct_renderer is not None:
            publisher.add_listener(RendererEventSubscriber(direct_renderer))

    def on_reasoning_delta(self, piece: str) -> None:
        self.publisher.publish("reasoning.delta", {"piece": piece})

    def on_content_delta(self, piece: str) -> None:
        self.publisher.publish("content.delta", {"piece": piece})

    def on_tool_call(self, tool_call: ToolCall | dict) -> None:
        call = tool_call if isinstance(tool_call, ToolCall) else ToolCall(
            name=str(tool_call.get("name", "tool")),
            arguments=dict(tool_call.get("arguments") or {}),
            id=str(tool_call.get("id", "")),
        )
        self._active_call_id = call.id
        self.publisher.publish("tool.planned", {
            "call_id": call.id, "name": call.name, "arguments": call.arguments,
            "phase": "planned",
        })

    def on_tool_phase(self, tool_call: ToolCall | dict, phase: str) -> None:
        if phase not in {"awaiting_approval", "running"}:
            raise ValueError(f"unknown tool phase: {phase}")
        call = tool_call if isinstance(tool_call, ToolCall) else ToolCall(
            name=str(tool_call.get("name", "tool")),
            arguments=dict(tool_call.get("arguments") or {}),
            id=str(tool_call.get("id", "")),
        )
        self.publisher.publish(f"tool.{phase}", {
            "call_id": call.id,
            "name": call.name,
            "arguments": call.arguments,
            "phase": phase,
        })

    def on_tool_output(self, call_id: str, line: str) -> None:
        self.publisher.publish("tool.output", {"call_id": call_id, "output": line})

    def on_command_output(self, line: str) -> None:
        self.on_tool_output(self._active_call_id or "", line)

    def on_tool_result(
        self, tool_call: ToolCall | dict, tool_result: ToolResult | dict,
    ) -> None:
        call = tool_call if isinstance(tool_call, ToolCall) else ToolCall(
            name=str(tool_call.get("name", "tool")),
            arguments=dict(tool_call.get("arguments") or {}),
            id=str(tool_call.get("id", "")),
        )
        result = tool_result.to_dict() if isinstance(tool_result, ToolResult) else dict(tool_result)
        self.publisher.publish("tool.finished", {
            "call_id": call.id, "name": call.name, **result,
        })

    def on_final(self, answer: Any) -> None:
        self.publisher.publish("content.final", {"content": _json_value(answer)})

    def on_turn_begin(self) -> None:
        self.publisher.publish(
            "session.status_changed", {"status": "model_turn_started"}
        )

    def on_completion_rejected(self, issues: Any = ()) -> None:
        self.publisher.publish("system.notice", {
            "kind": "completion_rejected", "issues": _json_value(issues),
        })

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        self.publisher.publish("system.notice", {
            "kind": "context_compact",
            "folded_count": folded_count,
            "prompt_tokens": prompt_tokens,
            "context_limit": context_limit,
            "context_watermark": context_watermark,
        })

    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        self.publisher.publish("usage.request", {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens, "context_limit": context_limit,
        })

    def on_usage_summary(
        self, prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        self.publisher.publish("usage.task", {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        })

    def on_checkpoint_error(self, error: str) -> None:
        self.publisher.publish("system.checkpoint_error", {"error": error})

    def on_agent_event(self, event: dict[str, Any]) -> None:
        self.publisher.publish("task.updated", event)

    def on_system_notice(self, text: str) -> None:
        self.publisher.publish("system.notice", {"text": text})

    def prompt_permission(
        self,
        tool_name: str,
        subject: str,
        risk_flags: str,
        reason: str,
        offer_always: bool,
        remember_rule: str = "",
        remember_persists: bool = False,
        revoke_hint: str = "",
    ) -> str:
        payload = {
            "tool_name": tool_name, "subject": subject, "risk_flags": risk_flags,
            "reason": reason, "offer_always": offer_always,
            "remember_rule": remember_rule,
            "remember_persists": remember_persists,
            "revoke_hint": revoke_hint,
        }
        if self.interaction is not None:
            return str(self.interaction.request("permission", payload))
        if self.direct_renderer is None:
            return "n"
        return self.direct_renderer.prompt_permission(**payload)

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None:
        payload = {"question": question, "context": context, "options": list(options)}
        if self.interaction is not None:
            answer = self.interaction.request("ask_user", payload)
            return str(answer) if answer is not None else None
        if self.direct_renderer is None:
            return None
        return self.direct_renderer.prompt_user(question, context, options)
