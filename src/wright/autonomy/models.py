"""Validated durable automation and concrete-run records."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Literal
from urllib.parse import urlparse


TriggerType = Literal[
    "once", "interval", "file_change", "web_change", "event"
]
AutomationStatus = Literal["active", "paused", "completed", "cancelled"]
RecoveryPolicy = Literal["manual", "retry"]
DurableRunStatus = Literal[
    "queued", "dispatched", "running", "waiting_retry", "completed", "failed",
    "cancelled", "unknown",
]
TERMINAL_DURABLE_RUN_STATUSES = frozenset({
    "completed", "failed", "cancelled", "unknown"
})


@dataclass(frozen=True)
class TriggerSpec:
    type: TriggerType
    run_at: float | None = None
    every_seconds: float | None = None
    start_at: float | None = None
    path: str = ""
    url: str = ""
    event_name: str = ""

    def __post_init__(self) -> None:
        if self.type == "once":
            _positive_number(self.run_at, "once.run_at", allow_zero=True)
        elif self.type == "interval":
            _positive_number(self.every_seconds, "interval.every_seconds")
            if self.start_at is not None:
                _positive_number(self.start_at, "interval.start_at", allow_zero=True)
        elif self.type == "file_change":
            if not self.path.strip() or len(self.path) > 2_000:
                raise ValueError("file_change.path must be a non-empty path")
        elif self.type == "web_change":
            if not self.url.strip() or len(self.url) > 2_000:
                raise ValueError("web_change.url must be a non-empty URL")
            parsed = urlparse(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "web_change.url must be an HTTP(S) URL without credentials"
                )
            _positive_number(self.every_seconds, "web_change.every_seconds")
        elif self.type == "event":
            if not self.event_name.strip() or len(self.event_name) > 200:
                raise ValueError("event.event_name must be non-empty and <= 200 chars")
        else:
            raise ValueError(f"unsupported trigger type: {self.type}")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"type": self.type}
        if self.type == "once":
            value["run_at"] = self.run_at
        elif self.type == "interval":
            value["every_seconds"] = self.every_seconds
            if self.start_at is not None:
                value["start_at"] = self.start_at
        elif self.type == "file_change":
            value["path"] = self.path
        elif self.type == "web_change":
            value["url"] = self.url
            value["every_seconds"] = self.every_seconds
        else:
            value["event_name"] = self.event_name
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "TriggerSpec":
        if not isinstance(value, dict):
            raise ValueError("trigger must be an object")
        trigger_type = value.get("type")
        if trigger_type not in {
            "once", "interval", "file_change", "web_change", "event"
        }:
            raise ValueError(f"unsupported trigger type: {trigger_type}")
        return cls(
            type=trigger_type,
            run_at=_optional_float(value.get("run_at")),
            every_seconds=_optional_float(value.get("every_seconds")),
            start_at=_optional_float(value.get("start_at")),
            path=str(value.get("path") or ""),
            url=str(value.get("url") or ""),
            event_name=str(value.get("event_name") or ""),
        )


@dataclass(frozen=True)
class AutomationRecord:
    id: str
    session_id: str
    name: str
    prompt: str
    trigger: TriggerSpec
    status: AutomationStatus
    recovery_policy: RecoveryPolicy
    max_retries: int
    retry_delay_seconds: float
    created_at: float
    updated_at: float
    next_run_at: float | None = None
    last_run_at: float | None = None
    trigger_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "name": self.name,
            "prompt": self.prompt,
            "trigger": self.trigger.to_dict(),
            "status": self.status,
            "recovery_policy": self.recovery_policy,
            "max_retries": self.max_retries,
            "retry_delay_seconds": self.retry_delay_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "trigger_state": dict(self.trigger_state),
        }


@dataclass(frozen=True)
class DurableRunRecord:
    id: str
    automation_id: str
    session_id: str
    automation_name: str
    prompt: str
    trigger_type: TriggerType
    trigger_payload: dict[str, Any]
    status: DurableRunStatus
    attempt: int
    max_retries: int
    scheduled_for: float
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    result: str = ""
    error: str = ""
    cancel_requested: bool = False
    cancel_reason: str = ""
    root_turn_id: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_DURABLE_RUN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "automation_id": self.automation_id,
            "session_id": self.session_id,
            "automation_name": self.automation_name,
            "prompt": self.prompt,
            "trigger_type": self.trigger_type,
            "trigger_payload": dict(self.trigger_payload),
            "status": self.status,
            "attempt": self.attempt,
            "max_retries": self.max_retries,
            "scheduled_for": self.scheduled_for,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "cancel_reason": self.cancel_reason,
            "root_turn_id": self.root_turn_id,
        }


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("trigger time values must be numbers")
    return float(value)


def _positive_number(
    value: float | None,
    name: str,
    *,
    allow_zero: bool = False,
) -> None:
    if (
        value is None
        or not math.isfinite(value)
        or value < 0
        or (not allow_zero and value == 0)
    ):
        comparator = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be {comparator}")
