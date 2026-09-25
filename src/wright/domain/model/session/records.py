"""Execution, turn, usage, and background records for Session."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

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
class UsageRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: Any) -> UsageRecord:
        """把 LLM 原始 usage 归一成 UsageRecord。

        原始 usage 形态不一:有的接口给 dict,有的给带属性的对象(SDK 模型),
        这种"形状差异"的知识收在这里,主循环不该操心。total 缺省时用
        prompt + completion 兜底。
        """
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total_tokens = usage.get("total_tokens")
        else:
            prompt_tokens = int(
                getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
            )
            completion_tokens = int(
                getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
            )
            total_tokens = getattr(usage, "total_tokens", None)

        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens

        return cls(prompt_tokens, completion_tokens, int(total_tokens or 0))


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


@dataclass
class BackgroundTask:
    task_id: str
    # reader 线程完成时调用；由 execute_command 在注册时写入，主循环
    # 可借此收到统一的 TASK_DONE(task_id) 通知。
    on_done: Callable[[], None] | None = field(default=None, repr=False)
    # TaskService 需要的通用元数据。执行状态仍由 process + done 唯一决定，
    # 这些字段只补充描述、归属和取消意图，不形成另一套状态机。
    command: str = ""
    root_turn_id: str = ""
    run_id: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""


__all__ = [
    "BackgroundTask",
    "CallId",
    "SessionLifecycle",
    "ToolExecutionRecord",
    "ToolExecutionStatus",
    "ToolExecutionTerminal",
    "TurnRecord",
    "TurnRoute",
    "UsageRecord",
    "VerificationRecord",
]
