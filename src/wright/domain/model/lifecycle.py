"""Lifecycle facts and hook decisions.

File traces and subprocess hooks are infrastructure. This module is the
contract both the orchestrator and those adapters share.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol
from uuid import uuid4

LifecycleEventName = Literal[
    "session_start",
    "runtime_event",
    "user_prompt_submit",
    "agent_start",
    "agent_stop",
    "llm_start",
    "llm_end",
    "llm_error",
    "pre_tool_use",
    "permission_decision",
    "post_tool_use",
    "tool_failure",
    "subagent_start",
    "subagent_stop",
    "pre_compact",
    "post_compact",
    "hook_result",
    "hook_error",
]

LIFECYCLE_EVENT_NAMES = frozenset(
    {
        "session_start",
        "runtime_event",
        "user_prompt_submit",
        "agent_start",
        "agent_stop",
        "llm_start",
        "llm_end",
        "llm_error",
        "pre_tool_use",
        "permission_decision",
        "post_tool_use",
        "tool_failure",
        "subagent_start",
        "subagent_stop",
        "pre_compact",
        "post_compact",
        "hook_result",
        "hook_error",
    }
)
HOOKABLE_EVENT_NAMES = LIFECYCLE_EVENT_NAMES - {"hook_result", "hook_error"}

BLOCKING_EVENTS = frozenset({"user_prompt_submit", "pre_tool_use", "agent_stop"})


class LifecycleConfigError(ValueError):
    pass


class HookExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HookDecision:
    decision: Literal["allow", "deny"] = "allow"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str = ""

    @classmethod
    def from_value(cls, value: Any) -> HookDecision:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise HookExecutionError("hook output must be a JSON object")
        decision = value.get("decision", "allow")
        if decision not in {"allow", "deny"}:
            raise HookExecutionError("hook decision must be 'allow' or 'deny'")
        updated_input = value.get("updated_input")
        if updated_input is not None and not isinstance(updated_input, dict):
            raise HookExecutionError("hook updated_input must be an object")
        return cls(
            decision=decision,
            reason=str(value.get("reason") or "")[:2_000],
            updated_input=dict(updated_input) if updated_input is not None else None,
            additional_context=str(value.get("additional_context") or "")[:8_000],
        )


@dataclass(frozen=True)
class LifecycleEvent:
    event: LifecycleEventName
    session_id: str
    sequence: int
    timestamp: float
    payload: dict[str, Any]
    agent_task_id: str | None = None
    root_turn_id: str = ""
    event_id: str = field(default_factory=lambda: uuid4().hex)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event": self.event,
            "session_id": self.session_id,
            "agent_task_id": self.agent_task_id,
            "root_turn_id": self.root_turn_id,
            "payload": _bounded_value(self.payload),
        }


HookCallback = Callable[[LifecycleEvent], HookDecision | Mapping[str, Any] | None]


@dataclass(frozen=True)
class HookRegistration:
    event: LifecycleEventName
    callback: HookCallback
    matcher: str = "*"
    name: str = "hook"

    def matches(self, payload: Mapping[str, Any]) -> bool:
        subject = str(payload.get("tool_name") or payload.get("agent_type") or "")
        return self.matcher == "*" or fnmatchcase(subject, self.matcher)


class TraceSink(Protocol):
    """Append-only lifecycle fact log."""

    def append(self, event: LifecycleEvent) -> None: ...

    def last_sequence(self) -> int: ...


def _join_context(left: str, right: str) -> str:
    if not right:
        return left
    return f"{left}\n{right}".strip()[:8_000]


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "[trace depth limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 8_000 else value[:8_000] + "[truncated]"
    if isinstance(value, Mapping):
        bounded = {}
        for key, item in list(value.items())[:100]:
            name = str(key)[:200]
            if any(
                marker in name.lower()
                for marker in (
                    "api_key",
                    "apikey",
                    "password",
                    "passwd",
                    "secret",
                    "authorization",
                    "cookie",
                    "access_token",
                    "refresh_token",
                )
            ):
                bounded[name] = "[redacted]"
            else:
                bounded[name] = _bounded_value(item, depth=depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth + 1) for item in value[:100]]
    if hasattr(value, "to_dict"):
        try:
            return _bounded_value(value.to_dict(), depth=depth + 1)
        except Exception:  # 脱敏失败则退回 repr
            pass
    return repr(value)[:2_000]
