"""Serializable session-event contract and the producer facade.

``EventPublisher`` lives in ``publisher``. User questions live in
``interaction``. This module does not import a renderer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from ...domain.model.tool import ToolCall, ToolResult
from .live_resources import RuntimeResources

if TYPE_CHECKING:
    from .publisher import EventPublisher


UI_EVENT_VERSION = 2
UI_EVENT_TYPES = frozenset({
    "session.snapshot", "session.status_changed", "turn.started",
    "turn.completed", "turn.failed", "turn.cancelled", "reasoning.delta",
    "content.delta", "content.final", "tool.planned", "tool.awaiting_approval",
    "tool.running", "tool.output", "tool.finished", "interaction.requested", "interaction.resolved",
    "task.updated", "usage.request", "usage.task", "system.notice",
    "system.checkpoint_error", "command.accepted", "command.rejected",
})


def notice_text(event_type: str, payload: dict[str, Any]) -> str:
    """Text shown for a notice. Snapshots and the web reducer use this wording."""

    if event_type == "turn.failed":
        detail = payload.get("error") or payload.get("status") or "unknown error"
        return f"Turn failed: {detail}"
    if event_type == "turn.cancelled":
        return "Turn cancelled."
    if event_type == "system.checkpoint_error":
        return f"Checkpoint failed: {payload.get('error') or 'unknown error'}"
    if event_type == "command.rejected":
        return str(payload.get("reason") or "Command rejected")
    if event_type == "task.updated":
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        return str(task.get("description") or payload.get("description") or "Background task updated")
    if payload.get("kind") == "completion_rejected":
        return "Completion check requested another attempt."
    if payload.get("kind") == "context_compact":
        folded = payload.get("folded_count") or 0
        return f"Context compacted ({int(folded)} items)."
    return str(payload.get("text") or "System notice")


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


@dataclass(frozen=True)
class EventScope:
    """Which agent produced an event. Depth 0 is the root session."""

    depth: int = 0
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise ValueError("event scope depth must be >= 0")


def _issue_payload(issues: Any) -> list[Any]:
    """Keep rejection messages after the event is serialized for subscribers."""
    payload: list[Any] = []
    for issue in issues or ():
        if isinstance(issue, dict):
            payload.append(issue)
        elif hasattr(issue, "to_dict"):
            payload.append(issue.to_dict())
        else:
            message = getattr(issue, "message", None)
            payload.append({"message": str(message if message is not None else issue)})
    return payload


def _as_tool_call(tool_call: ToolCall | dict) -> ToolCall:
    if isinstance(tool_call, ToolCall):
        return tool_call
    return ToolCall(
        name=str(tool_call.get("name", "tool")),
        arguments=dict(tool_call.get("arguments") or {}),
        id=str(tool_call.get("id", "")),
    )


class SessionEvents:
    """Producer-side UI stream. Callers publish events; renderers only subscribe.

    Session bookkeeping (stream text, tool phase) happens here, before the
    event is handed to any interface. Questions are not events.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        *,
        runtime_resources: RuntimeResources | None = None,
        scope: EventScope | None = None,
    ) -> None:
        self.publisher = publisher
        self.runtime_resources = runtime_resources
        self.scope = scope or EventScope()
        self._active_call_id: str | None = None

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        body = dict(payload or {})
        if self.scope.depth > 0:
            body.setdefault("agent_depth", self.scope.depth)
            body.setdefault("agent_task_id", self.scope.task_id)
        if event_type == "tool.planned":
            self._active_call_id = str(body.get("call_id", ""))
        self._record(event_type, body)
        self.publisher.publish(event_type, body)

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        resources = self.runtime_resources
        if resources is None:
            return
        responses = resources.responses
        if event_type == "reasoning.delta":
            responses.append_reasoning(str(payload.get("piece", "")))
        elif event_type == "content.delta":
            responses.append_content(str(payload.get("piece", "")))
        elif event_type == "content.final":
            responses.set_content(str(payload.get("content", "")))
        elif event_type in {"tool.planned", "tool.awaiting_approval", "tool.running"}:
            call_id = str(payload.get("call_id", ""))
            responses.update_tool(call_id, dict(payload))
        elif event_type == "tool.output":
            responses.append_tool_output(
                str(payload.get("call_id", "")), str(payload.get("output", ""))
            )
        elif event_type == "tool.finished":
            responses.update_tool(str(payload.get("call_id", "")), dict(payload))

    def on_reasoning_delta(self, piece: str) -> None:
        self.emit("reasoning.delta", {"piece": piece})

    def on_content_delta(self, piece: str) -> None:
        self.emit("content.delta", {"piece": piece})

    def on_tool_call(self, tool_call: ToolCall | dict) -> None:
        call = _as_tool_call(tool_call)
        self.emit("tool.planned", {
            "call_id": call.id, "name": call.name, "arguments": call.arguments,
            "phase": "planned",
        })

    def on_tool_phase(self, tool_call: ToolCall | dict, phase: str) -> None:
        if phase not in {"awaiting_approval", "running"}:
            raise ValueError(f"unknown tool phase: {phase}")
        call = _as_tool_call(tool_call)
        self.emit(f"tool.{phase}", {
            "call_id": call.id, "name": call.name, "arguments": call.arguments,
            "phase": phase,
        })

    def on_tool_output(self, call_id: str, line: str) -> None:
        self.emit("tool.output", {"call_id": call_id, "output": line})

    def on_command_output(self, line: str) -> None:
        self.on_tool_output(self._active_call_id or "", line)

    def on_tool_result(
        self, tool_call: ToolCall | dict, tool_result: ToolResult | dict,
    ) -> None:
        call = _as_tool_call(tool_call)
        result = tool_result.to_dict() if isinstance(tool_result, ToolResult) else dict(tool_result)
        self.emit("tool.finished", {"call_id": call.id, "name": call.name, **result})

    def on_final(self, answer: Any) -> None:
        self.emit("content.final", {"content": _json_value(answer)})

    def on_turn_begin(self) -> None:
        self.emit("session.status_changed", {"status": "model_turn_started"})

    def on_completion_rejected(self, issues: Any = ()) -> None:
        self.emit("system.notice", {
            "kind": "completion_rejected", "issues": _issue_payload(issues),
        })

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        self.emit("system.notice", {
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
        context_tokens: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens, "context_limit": context_limit,
        }
        if context_tokens is not None:
            payload["context_tokens"] = context_tokens
        self.emit("usage.request", payload)

    def on_usage_summary(
        self, prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        self.emit("usage.task", {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        })

    def on_checkpoint_error(self, error: str) -> None:
        self.emit("system.checkpoint_error", {"error": error})

    def on_agent_event(self, event: dict[str, Any]) -> None:
        self.emit("task.updated", dict(event))

    def on_system_notice(self, text: str) -> None:
        self.emit("system.notice", {"text": text})
