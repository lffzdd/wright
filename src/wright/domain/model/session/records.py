"""Session steps and in-session tool execution records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from ..llm.usage import UsageRecord
from ..tool import ToolCall, ToolResult
from .conversation import MessageId

CallId: TypeAlias = str
SessionLifecycle = Literal["open", "closing", "closed"]
TurnRoute = Literal["tool_calls", "final", "invalid"]
ToolExecutionTerminal = Literal["succeeded", "failed", "timeout"]
ToolExecutionStatus = Literal["pending", "running"] | ToolExecutionTerminal


@dataclass
class ToolExecutionRecord:
    call: ToolCall
    result: ToolResult | None
    step: int
    status: ToolExecutionStatus
    started_at: float | None = None
    ended_at: float | None = None
    run_id: str = ""
    step_id: str = ""


@dataclass
class VerificationRecord:
    approved: bool
    issues: list[dict[str, str]]


@dataclass
class TurnRecord:
    step: int
    message_id: MessageId  # 指向这轮 assistant wire 记录,引用而非复制原文
    parsed: dict
    route: TurnRoute

    tool_execution_ids: list[CallId]
    error: str | None = None

    usage: UsageRecord | None = None
    verification: VerificationRecord | None = None
    # ModelStep identity and ownership; TurnRecord remains the compatibility name.
    run_id: str = ""
    step_id: str = ""


__all__ = [
    "CallId",
    "SessionLifecycle",
    "ToolExecutionRecord",
    "ToolExecutionStatus",
    "ToolExecutionTerminal",
    "TurnRecord",
    "TurnRoute",
    "VerificationRecord",
]
