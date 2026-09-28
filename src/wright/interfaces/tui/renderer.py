"""Fullscreen TUI renderer: Agent events → Textual messages, tests without an App.

Does not reuse ConsoleRenderer Live / freeze / prompt_toolkit. Input and
permission collection belong to the Textual app, not this class.
"""

from __future__ import annotations

import threading
from typing import Any

from ...domain.model.tool import ToolCall, ToolResult
from ..i18n import format_issues, t
from ..rendering.contracts import Renderer
from .messages import (
    AgentEventNotice,
    DraftFreeze,
    FinalAnswer,
    RequestUsage,
    StatusChanged,
    StreamRefresh,
    SystemNotice,
    TaskUsage,
    ToolUpsert,
    TurnBegin,
)
from .view_models import (
    ToolView,
    stringify_answer,
    tool_call_args,
    tool_call_id,
    tool_call_name,
)


class TUIRenderer(Renderer):
    """Projects Agent `on_*` callbacks into Textual messages.

    Safe without an attached App: tests can read `content`, `tools`, `final_answer`.
    """

    _attached = 0

    def __init__(self) -> None:
        self._target: Any | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._refresh_posted = False
        self._tool_seq = 0
        self.reasoning = ""
        self.content = ""
        self.final_answer: Any = None
        self.tools: list[ToolView] = []
        # These are distinct views of usage. Context is the latest server-reported
        # request input, never an estimate of the next request.
        self.request_usage = ""
        self.task_usage = ""
        self._pending_request_usage = ""
        self.context_tokens: int | None = None
        self.context_limit: int | None = None
        self.notices: list[str] = []

    def attach(self, target: Any) -> None:
        if self._target is None:
            type(self)._attached += 1
        self._target = target
        self._closed = False

    def detach(self) -> None:
        if self._target is not None:
            type(self)._attached = max(0, type(self)._attached - 1)
        self._closed = True
        self._target = None

    @classmethod
    def is_active(cls) -> bool:
        return cls._attached > 0

    def _emit(self, message: Any) -> None:
        target = self._target
        if self._closed or target is None:
            return
        try:
            target.post_message(message)
        except Exception:
            pass

    def _post_stream_refresh(self) -> None:
        with self._lock:
            if self._refresh_posted:
                return
            self._refresh_posted = True
        self._emit(StreamRefresh())

    def consume_stream(self) -> tuple[str, str]:
        with self._lock:
            self._refresh_posted = False
            return self.reasoning, self.content

    def on_turn_begin(self) -> None:
        with self._lock:
            self.reasoning = ""
            self.content = ""
            self.final_answer = None
            self._refresh_posted = False
            self._pending_request_usage = ""
        self._emit(TurnBegin())

    def _flush_request_usage(self) -> None:
        with self._lock:
            text = self._pending_request_usage
            self._pending_request_usage = ""
        if text:
            self._emit(RequestUsage(text))

    def on_reasoning_delta(self, piece: str) -> None:
        if not piece:
            return
        with self._lock:
            self.reasoning += piece
        self._post_stream_refresh()

    def on_content_delta(self, piece: str) -> None:
        if not piece:
            return
        with self._lock:
            self.content += piece
        self._post_stream_refresh()

    def on_tool_call(self, tool_call: ToolCall | dict) -> None:
        freeze = ""
        with self._lock:
            freeze = self.content
            self.reasoning = ""
            self.content = ""
            self._refresh_posted = False
            self._tool_seq += 1
            key = tool_call_id(tool_call) or f"tool-{self._tool_seq}"
            view = ToolView(
                key=key,
                name=tool_call_name(tool_call),
                arguments=tool_call_args(tool_call),
            )
            self.tools.append(view)
        if freeze:
            self._emit(DraftFreeze(freeze))
        self._flush_request_usage()
        self._emit(ToolUpsert(view))

    def on_tool_phase(self, tool_call: ToolCall | dict, phase: str) -> None:
        call_id = tool_call_id(tool_call)
        with self._lock:
            view = next((item for item in self.tools if item.key == call_id), None)
            if view is None:
                return
            view.status = phase
        self._emit(ToolUpsert(view))

    def on_command_output(self, line: str) -> None:
        with self._lock:
            running = [
                block for block in self.tools
                if block.status == "running" and block.name == "execute_command"
            ]
            if not running:
                return
            block = running[-1]
            block.output += line
            view = block
        self._emit(ToolUpsert(view))

    def on_tool_result(
        self, tool_call: ToolCall | dict, tool_result: ToolResult | dict,
    ) -> None:
        if hasattr(tool_result, "to_dict"):
            tool_result = tool_result.to_dict()
        call_id = tool_call_id(tool_call)
        name = tool_call_name(tool_call)
        with self._lock:
            block = None
            if call_id:
                for item in self.tools:
                    if item.key == call_id:
                        block = item
                        break
            if block is None:
                running = [
                    item for item in self.tools
                    if item.status == "running" and item.name == name
                ]
                block = running[-1] if running else None
            if block is None:
                self._tool_seq += 1
                block = ToolView(
                    key=call_id or f"tool-{self._tool_seq}",
                    name=name,
                    arguments=tool_call_args(tool_call),
                    status="running",
                )
                self.tools.append(block)
            ok = bool(tool_result.get("ok")) if isinstance(tool_result, dict) else False
            block.status = "done" if ok else "error"
            if isinstance(tool_result, dict):
                if ok:
                    block.result = tool_result.get("data")
                    block.error = ""
                else:
                    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
                    block.display_code = str(data.get("display_code") or "")
                    params = data.get("display_params") if isinstance(data.get("display_params"), dict) else {}
                    block.display_params = {str(key): str(value) for key, value in params.items()}
                    block.error = str(tool_result.get("err") or "Unknown error")
                    block.result = None
            view = block
        self._emit(ToolUpsert(view))

    def on_final(self, answer: Any) -> None:
        text = stringify_answer(answer)
        with self._lock:
            self.final_answer = answer
            self.content = text
            self._refresh_posted = False
        self._emit(FinalAnswer(text))
        self._flush_request_usage()

    def on_completion_rejected(self, issues: Any = ()) -> None:
        detail = format_issues(issues)
        freeze = ""
        with self._lock:
            freeze = self.content
            self.reasoning = ""
            self.content = ""
            self._refresh_posted = False
        if freeze:
            self._emit(DraftFreeze(freeze))
        self._flush_request_usage()
        self.on_system_notice(t("verification.rejected", detail=detail))

    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        inp = f"{prompt_tokens:,}" if prompt_tokens is not None else "?"
        out = f"{completion_tokens:,}" if completion_tokens is not None else "?"
        with self._lock:
            self.request_usage = f"{inp} in  ·  {out} out"
            self._pending_request_usage = self.request_usage
            self.context_tokens = prompt_tokens
            self.context_limit = context_limit
        self._emit(StatusChanged())

    def on_usage_summary(
        self, prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        with self._lock:
            self.task_usage = (
                f"task total  ·  {prompt_tokens:,} in  ·  "
                f"{completion_tokens:,} out  ·  {total_tokens:,} total"
            )
            detail = (
                "Task usage:\n"
                f"{prompt_tokens:,} in\n"
                f"{completion_tokens:,} out\n"
                f"{total_tokens:,} total"
            )
        total_short = f"{total_tokens / 1_000:.1f}k" if total_tokens >= 1_000 else str(total_tokens)
        self._emit(TaskUsage(f"usage  Σ {total_short}", detail))

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        if prompt_tokens is not None and context_limit:
            ctx = f"{prompt_tokens:,}/{context_limit:,}"
        else:
            ctx = "?/?"
        if folded_count > 0:
            msg = f"context compact: folded {folded_count} · {ctx}"
        else:
            msg = f"context compact: nothing to fold · {ctx}"
        self.on_system_notice(msg)

    def on_checkpoint_error(self, error: str) -> None:
        self.on_system_notice(f"checkpoint failed: {error}")

    def on_agent_event(self, event: dict[str, Any]) -> None:
        status = str(event.get("status", "unknown"))
        task_id = str(event.get("task_id", "?"))
        depth = event.get("depth", "?")
        task = str(event.get("task", ""))
        if len(task) > 80:
            task = task[:77] + "…"
        line = f"agent {task_id[:8]} · d{depth} · {status}"
        if task:
            line += f"  {task}"
        with self._lock:
            self.notices.append(line)
        self._emit(AgentEventNotice(event, line))

    def on_system_notice(self, text: str, *, code: str = "", params: dict | None = None) -> None:
        del code, params
        with self._lock:
            self.notices.append(text)
        self._emit(SystemNotice(text))
