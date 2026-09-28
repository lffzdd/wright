"""Output contract shared by CLI, TUI, and headless hosts.

Importing this module does not load Rich, Textual, or prompt_toolkit.
Questions and main input are not part of this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ...domain.model.tool import ToolCall, ToolResult


class Renderer(ABC):
    """Display events produced by a session. It does not collect input."""

    @abstractmethod
    def on_reasoning_delta(self, piece: str) -> None: ...
    @abstractmethod
    def on_content_delta(self, piece: str) -> None: ...
    @abstractmethod
    def on_tool_call(self, tool_call: ToolCall | dict) -> None: ...
    @abstractmethod
    def on_tool_result(
        self, tool_call: ToolCall | dict, tool_result: ToolResult | dict,
    ) -> None: ...
    @abstractmethod
    def on_final(self, answer: Any) -> None: ...

    def on_turn_begin(self) -> None:
        """A new LLM turn is starting. Default: no-op."""

    def on_completion_rejected(self, issues: Any = ()) -> None:
        """Structural completion checks rejected this candidate. Default: no-op."""

    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        """本轮 token 用量回调(服务端精确值)。默认不输出，子类按需覆盖。"""

    def on_usage_summary(
        self, prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        """当前任务累计消费。"""

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        """上下文压缩回调。默认不输出，子类按需覆盖。"""

    def on_command_output(self, line: str) -> None:
        """命令流式输出回调。默认不输出，子类按需覆盖。"""

    def on_tool_output(self, call_id: str, line: str) -> None:
        """Stable-id tool output hook; legacy renderers receive the same line."""
        self.on_command_output(line)

    def on_tool_phase(self, tool_call: ToolCall | dict, phase: str) -> None:
        """A planned tool moved to awaiting_approval or running."""

    def on_checkpoint_error(self, error: str) -> None:
        """Checkpoint 持久化失败。默认不输出，交互渲染器应明确告警。"""

    def on_agent_event(self, event: dict[str, Any]) -> None:
        """子 Agent 控制面事件。默认不输出。"""

    def on_system_notice(self, text: str, *, code: str = "", params: dict | None = None) -> None:
        """Host-level status line (slash feedback, durable run, autonomy). Default: no-op."""
