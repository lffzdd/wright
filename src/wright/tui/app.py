"""Textual fullscreen host. Owns the event loop; Agent runs on a worker thread."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from queue import Full
from typing import Any, ClassVar

from rich.console import Group
from rich.json import JSON as RichJSON
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Static, TextArea

from ..interaction import InteractionRequest
from ..logger import get_logger
from ..runtime import WrightRuntime, build_runtime, shutdown_runtime
from ..session_host import process_session_event
from .renderer import (
    AgentEventNotice,
    DraftFreeze,
    FinalAnswer,
    InteractionNeeded,
    RequestUsage,
    StatusChanged,
    StreamRefresh,
    SystemNotice,
    TaskUsage,
    ToolUpsert,
    ToolView,
    TUIRenderer,
    TurnBegin,
)
from .session_control import (
    SessionControlRequest,
    available_models,
    runtime_args_for_transition,
)
from .slash import SlashCompletion, tui_help_text

logger = get_logger(__name__)

_COMMAND_OUTPUT_LINES = 24


def require_interactive_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("TUI 需要交互式终端（stdin 与 stdout 均为 TTY）")


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _fail_interaction(request: InteractionRequest) -> None:
    value: Any = None if request.kind == "ask_user" else "n"
    try:
        request.reply.put_nowait(value)
    except Full:
        pass


def _context_ring(tokens: int | None, limit: int | None) -> tuple[str, str, str]:
    """Format the current session context for a compact indicator and tooltip."""
    if tokens is None or not limit:
        return "○", "等待上下文", "Context window:\nWaiting for a context limit"
    ratio = max(0.0, min(tokens / limit, 1.0))
    ring = ("○", "◔", "◑", "◕", "●")[min(4, int(ratio * 4.999))]
    compact = f"{ring}  {ratio:.0%}"
    detail = (
        f"Context window:\n{ratio:.0%} full\n"
        f"{tokens:,} / {limit:,} tokens used\n\n"
        "Current session context"
    )
    return compact, detail, "warning" if ratio >= 0.75 else "normal"


def _short_tokens(tokens: int) -> str:
    return f"{tokens / 1_000:.1f}k" if tokens >= 1_000 else str(tokens)


def _task_usage_detail(prompt: int, completion: int, total: int) -> str:
    return (
        "Task usage:\n"
        f"{prompt:,} in\n"
        f"{completion:,} out\n"
        f"{total:,} total"
    )


class UserBlock(Static):
    def __init__(self, text: str) -> None:
        content = Text()
        content.append("❯ ", style="bold cyan")
        content.append(text)
        super().__init__(content, classes="msg user")


def _format_assistant_text(text: str, *, draft: bool) -> Any:
    if not text:
        return "…" if draft else ""
    if draft:
        return text
    try:
        return RichMarkdown(text)
    except Exception:
        return text


class AssistantBlock(Static):
    def __init__(self, text: str = "", *, draft: bool = False) -> None:
        classes = "msg assistant draft" if draft else "msg assistant"
        super().__init__(_format_assistant_text(text, draft=draft), classes=classes)

    def set_draft(self, text: str) -> None:
        self.set_classes("msg assistant draft")
        self.update(_format_assistant_text(text, draft=True))

    def set_final(self, text: str) -> None:
        self.set_classes("msg assistant")
        self.update(_format_assistant_text(text, draft=False))


class ReasoningBlock(Collapsible):
    """A visible, in-place view of the model's streamed reasoning."""

    def __init__(self, text: str = "", *, collapsed: bool = False) -> None:
        self._body = Static(text or "…", classes="reasoning-body")
        super().__init__(
            self._body,
            title="思考" if text else "思考中",
            collapsed=collapsed,
            classes="reasoning",
        )

    def update_reasoning(self, text: str) -> None:
        self._body.update(text or "…")


class SubagentBlock(Static):
    def __init__(self, data: dict[str, Any], text: str) -> None:
        status = str(data.get("status", "unknown"))
        depth = data.get("depth", "?")
        task_id = str(data.get("task_id", "?"))[:8]
        task = str(data.get("task", ""))
        if len(task) > 80:
            task = task[:77] + "…"

        line = Text()
        line.append("🤖 SubAgent ", style="bold magenta")
        line.append(f"[{task_id}] ", style="bold white")
        line.append(f"d{depth} ", style="dim")
        status_style = {
            "running": "cyan bold",
            "completed": "green bold",
            "failed": "red bold",
        }.get(status, "yellow")
        line.append(f"· {status}", style=status_style)
        if task:
            line.append(f"  {task}", style="dim")
        super().__init__(line, classes=f"subagent {status}")


class SystemBlock(Static):
    def __init__(self, text: str) -> None:
        super().__init__(text, classes="msg system")


class UsageBlock(Static):
    def __init__(self, text: str, *, total: bool = False) -> None:
        classes = "usage total" if total else "usage"
        super().__init__(text, classes=classes)


class TaskUsageBlock(UsageBlock):
    def __init__(self, summary: str, detail: str) -> None:
        super().__init__(summary, total=True)
        self.tooltip = detail


class ToolBlock(Collapsible):
    def __init__(self, tool: ToolView) -> None:
        self._body = Static(_tool_body(tool), classes="tool-body")
        super().__init__(
            self._body,
            title=_tool_title(tool),
            collapsed=True,
            classes=f"tool {_tool_class(tool)}",
        )
        self.tool_key = tool.key

    def apply(self, tool: ToolView) -> None:
        self.tool_key = tool.key
        self.title = _tool_title(tool)
        self.set_classes(f"tool {_tool_class(tool)}")
        self._body.update(_tool_body(tool))


def _tool_class(tool: ToolView) -> str:
    if tool.status == "error":
        return "error"
    if tool.status == "done":
        return "done"
    if tool.status == "awaiting_approval":
        return "awaiting"
    return "running"


def _tool_arg_summary(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        if isinstance(args, str) and args.strip():
            return args.strip()[:36]
        return ""
    if "command" in args and isinstance(args["command"], str):
        cmd = args["command"].strip().replace("\n", " ")
        return f"$ {cmd[:36]}…" if len(cmd) > 36 else f"$ {cmd}"
    if name == "edit_file" and isinstance(args.get("file"), str):
        return args["file"]
    for key in ("path", "file_path", "file", "TargetFile", "AbsolutePath", "SearchDirectory"):
        if key in args and isinstance(args[key], str):
            p = args[key].strip()
            parts = p.split("/")
            return "/".join(parts[-2:]) if len(parts) > 2 else p
    for key in ("query", "Query", "pattern", "Pattern"):
        if key in args and isinstance(args[key], str):
            q = args[key].strip()
            return f'"{q[:30]}…"' if len(q) > 30 else f'"{q}"'
    for key in ("task", "instruction", "Instruction", "prompt"):
        if key in args and isinstance(args[key], str):
            s = args[key].strip().replace("\n", " ")
            return s[:36] + "…" if len(s) > 36 else s
    for v in args.values():
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("\n", " ")
            return s[:32] + "…" if len(s) > 32 else s
    return ""


def _tool_title(tool: ToolView) -> str:
    icon = {
        "planned": "○",
        "awaiting_approval": "⚠",
        "running": "⏳",
        "done": "✓",
        "error": "✗",
    }.get(tool.status, "○")
    summary = _tool_arg_summary(tool.name, tool.arguments)
    if summary:
        return f"{icon} {tool.name} · {summary}"
    return f"{icon} {tool.name} · {tool.status}"


def _format_diff(diff_text: str) -> Text:
    t = Text()
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            t.append(line + "\n", style="green")
        elif line.startswith("-") and not line.startswith("---"):
            t.append(line + "\n", style="red")
        elif line.startswith("@@"):
            t.append(line + "\n", style="cyan")
        else:
            t.append(line + "\n", style="dim")
    return t


def _tool_body(tool: ToolView) -> Any:
    parts: list[Any] = []
    args = tool.arguments
    if args:
        if isinstance(args, dict) and "command" in args and isinstance(args["command"], str):
            parts.append(Text(f"$ {args['command']}", style="bold cyan"))
        elif isinstance(args, dict) and tool.name == "edit_file":
            old_text = str(args.get("old_text") or "")
            new_text = str(args.get("new_text") or "")
            diff = "\n".join(
                [
                    *(f"-{line}" for line in old_text.splitlines() or [""]),
                    *(f"+{line}" for line in new_text.splitlines() or [""]),
                ]
            )
            if old_text or new_text:
                parts.append(_format_diff(diff))
            else:
                parts.append(Text("(no changes)", style="dim italic"))
        else:
            try:
                parts.append(Group(Text("参数:", style="bold dim"), RichJSON.from_data(args)))
            except Exception:
                parts.append(Text(_json_text(args), style="dim"))

    if tool.output:
        lines = tool.output.splitlines()
        clipped = lines[-_COMMAND_OUTPUT_LINES:]
        prefix = "" if len(lines) <= _COMMAND_OUTPUT_LINES else "…\n"
        body_txt = prefix + "\n".join(clipped)
        if any(l.startswith("@@") or (l.startswith("+") and not l.startswith("+++")) or (l.startswith("-") and not l.startswith("---")) for l in clipped):
            parts.append(_format_diff(body_txt))
        else:
            parts.append(Text(body_txt, style="dim"))

    if tool.status == "error" and tool.error:
        parts.append(Text(f"错误: {tool.error}", style="bold red"))
    elif tool.result is not None:
        if isinstance(tool.result, (dict, list)):
            try:
                parts.append(Group(Text("返回结果:", style="bold dim"), RichJSON.from_data(tool.result)))
            except Exception:
                parts.append(Text(_json_text(tool.result), style="dim"))
        else:
            parts.append(Text(str(tool.result), style="dim"))

    if not parts:
        return Text("(no payload)", style="dim italic")
    if len(parts) == 1:
        return parts[0]
    return Group(*parts)


class PermissionModal(ModalScreen[str]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "allow", "Allow", show=True),
        Binding("n", "deny", "Deny", show=True),
        Binding("a", "always", "Always", show=True),
        Binding("escape", "deny", "Deny", show=False),
    ]

    def __init__(
        self,
        tool_name: str,
        subject: str,
        risk_flags: str,
        reason: str,
        offer_always: bool,
        remember_rule: str = "",
        remember_persists: bool = False,
        revoke_hint: str = "",
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.subject = subject
        self.risk_flags = risk_flags
        self.reason = reason
        self.offer_always = offer_always
        self.remember_rule = remember_rule
        self.remember_persists = remember_persists
        self.revoke_hint = revoke_hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("permission", classes="dialog-kicker")
            yield Static(self.tool_name, classes="dialog-title")
            if self.subject:
                yield Static(self.subject, classes="dialog-subject")
            yield Static(f"risk  {self.risk_flags}", classes="dialog-meta")
            yield Static(self.reason, classes="dialog-reason")
            if self.offer_always and self.remember_rule:
                persistence = "cross-session" if self.remember_persists else "this session"
                yield Static(
                    f"scope  {self.remember_rule}\npersistence  {persistence}",
                    classes="dialog-meta",
                )
                if self.revoke_hint:
                    yield Static(f"revoke  {self.revoke_hint}", classes="dialog-reason")
            with Horizontal(classes="dialog-actions"):
                yield Button("allow", id="allow", variant="success")
                yield Button("deny", id="deny", variant="error")
                if self.offer_always:
                    yield Button("always", id="always", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "allow":
            self.dismiss("y")
        elif event.button.id == "always":
            self.dismiss("a")
        else:
            self.dismiss("n")

    def action_allow(self) -> None:
        self.dismiss("y")

    def action_deny(self) -> None:
        self.dismiss("n")

    def action_always(self) -> None:
        self.dismiss("a" if self.offer_always else "n")


class AskUserModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.question = question
        self.context = context
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("question", classes="dialog-kicker")
            yield Static(self.question, classes="dialog-title")
            if self.context:
                yield Static(self.context, classes="dialog-reason")
            if self.options:
                with Vertical(classes="dialog-options"):
                    for idx, option in enumerate(self.options, start=1):
                        yield Button(f"{idx}. {option}", id=f"opt-{idx}", variant="primary")
            yield Input(placeholder="type an answer", id="answer")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if ident.startswith("opt-"):
            try:
                idx = int(ident.split("-", 1)[1]) - 1
            except ValueError:
                return
            if 0 <= idx < len(self.options):
                self.dismiss(self.options[idx])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ResumeModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("resume", classes="dialog-kicker")
            yield Static("Saved sessions", classes="dialog-title")
            with Vertical(classes="dialog-options"):
                for index, session in enumerate(self.sessions):
                    goal = str(session.get("user_goal") or "(no goal)").replace("\n", " ")
                    label = f"{session['session_id']}  ·  {goal[:52]}"
                    yield Button(label, id=f"session-{index}", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if not ident.startswith("session-"):
            return
        try:
            session = self.sessions[int(ident.removeprefix("session-"))]
        except (ValueError, IndexError):
            return
        self.dismiss(str(session["session_id"]))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, models: tuple[str, ...], current: str) -> None:
        super().__init__()
        self.models = models
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("model", classes="dialog-kicker")
            yield Static(f"Current: {self.current}", classes="dialog-title")
            if self.models:
                with Vertical(classes="dialog-options"):
                    for index, model in enumerate(self.models):
                        yield Button(model, id=f"model-{index}", variant="primary")
            yield Input(placeholder="type a model ID", id="model-input")

    def on_mount(self) -> None:
        self.query_one("#model-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if not ident.startswith("model-"):
            return
        try:
            self.dismiss(self.models[int(ident.removeprefix("model-"))])
        except (ValueError, IndexError):
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MultilineComposer(TextArea):
    """A chat composer that grows with its content and submits on Enter."""

    _MIN_VISIBLE_ROWS = 3
    _MAX_VISIBLE_ROWS = 6
    _FRAME_ROWS = 2

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class SlashChanged(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class SlashNavigate(Message):
        def __init__(self, offset: int) -> None:
            super().__init__()
            self.offset = offset

    class SlashComplete(Message):
        pass

    class SlashDismissed(Message):
        pass

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(show_line_numbers=False, **kwargs)
        self._slash_menu_open = False

    def set_slash_menu_open(self, value: bool) -> None:
        self._slash_menu_open = value

    def on_mount(self) -> None:
        self._fit_height()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self:
            self._fit_height()
            self.post_message(self.SlashChanged(self.text))

    def _fit_height(self) -> None:
        rows = min(
            self._MAX_VISIBLE_ROWS,
            max(self._MIN_VISIBLE_ROWS, self.wrapped_document.height),
        )
        self.styles.height = rows + self._FRAME_ROWS

    def on_key(self, event: Key) -> None:
        # iTerm2's xterm modifyOtherKeys protocol represents Shift+Enter as
        # ``shift+\\r``; Textual's Kitty protocol calls it ``shift+enter``.
        if event.key in {"shift+enter", "shift+\r", "ctrl+j"}:
            event.prevent_default()
            event.stop()
            start, end = self.selection
            self.replace("\n", start, end, maintain_selection_offset=False)
            return
        if self._slash_menu_open:
            if event.key == "up":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashNavigate(-1))
                return
            if event.key == "down":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashNavigate(1))
                return
            if event.key == "tab":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashComplete())
                return
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashDismissed())
                return
        if event.key != "enter":
            return
        event.prevent_default()
        event.stop()
        value = self.text.strip()
        if not value:
            return
        self.clear()
        self.post_message(self.Submitted(value))


class WrightTUI(App):
    """Fullscreen Wright session. Input stays pinned; Agent runs off-thread."""

    TITLE = "wright"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        background: #14181d;
        color: #dce3eb;
    }

    #status {
        dock: top;
        height: 3;
        background: #191f27;
        color: #9aa9ba;
        padding: 1 2;
    }

    #brand {
        width: auto;
        color: #a9c7ee;
        text-style: bold;
    }

    #status-model {
        width: auto;
        padding: 0 1;
        color: #bfa8e6;
    }

    #status-workspace {
        width: auto;
        padding: 0 1;
        color: #8da4be;
    }

    #status-state {
        width: auto;
        padding: 0 1;
        color: #94b9ae;
    }

    #status-state.running {
        color: #d7bb82;
        text-style: bold;
    }

    #status-meta {
        width: 1fr;
        color: #9aa9ba;
        text-align: right;
    }

    #context {
        width: auto;
        padding-left: 2;
        color: #94c9b2;
    }

    #context.warning {
        color: #c4a35a;
    }

    #transcript {
        height: 1fr;
        padding: 1 3;
        scrollbar-size: 1 1;
        scrollbar-background: #14181d;
        scrollbar-color: #354456;
        scrollbar-color-hover: #6682a1;
    }

    #composer-wrap {
        dock: bottom;
        height: auto;
        background: #14181d;
        padding: 0 2 1 2;
    }

    #composer {
        height: 5;
        min-height: 5;
        max-height: 8;
        background: #191f27;
        border: round #354456;
        padding: 0 1;
    }

    #composer:focus {
        border: round #83a9d4;
    }

    #composer-hint {
        height: 1;
        padding: 0 2;
        color: #8091a5;
    }

    #slash-suggestions {
        display: none;
        height: auto;
        max-height: 8;
        margin: 0 1;
        padding: 0 1;
        background: #202a35;
        border: round #52718f;
        color: #b9c8d8;
    }

    .msg {
        height: auto;
        margin: 0 0 1 0;
        padding: 0 0 0 1;
    }

    .user {
        color: #c3d8f2;
        background: #1c2734;
        border-left: thick #83a9d4;
        padding: 1 2;
    }

    .assistant {
        color: #dce3eb;
        border-left: solid #527767;
        padding: 0 2;
    }

    .assistant.draft {
        color: #bdcbdc;
        border-left: solid #6682a1;
    }

    .reasoning {
        height: auto;
        margin: 0 0 1 1;
        color: #a4afc2;
        background: #191f27;
        border: none;
        border-left: solid #696b88;
        padding: 0;
    }

    .reasoning-body {
        color: #a4afc2;
        padding: 0 1 1 1;
    }

    .subagent {
        height: auto;
        margin: 0 0 1 1;
        background: #171d26;
        border-left: solid #7c6f9e;
        padding: 0 1;
    }

    .subagent.running {
        border-left: solid #68a0cf;
    }

    .subagent.completed {
        border-left: solid #5da984;
    }

    .subagent.failed {
        border-left: solid #cf6868;
    }

    .system {
        color: #94a2b3;
        border-left: solid #354456;
        text-style: italic;
    }

    .usage {
        height: auto;
        margin: 0 0 1 2;
        color: #8999ad;
    }

    .usage.total {
        color: #a1bbd8;
        width: auto;
        padding: 0 1;
        background: #202c39;
        margin: 0 0 1 2;
    }

    .usage.total:hover {
        background: #2b3c4e;
        color: #dce3eb;
    }

    .tool {
        height: auto;
        margin: 0 0 1 1;
        background: #191f27;
        border: none;
        border-left: solid #354456;
        padding: 0;
    }

    .tool.running {
        color: #d7bb82;
    }

    .tool.done {
        color: #94c9b2;
    }

    .tool.error {
        color: #e49b9b;
    }

    .tool-body {
        color: #b0bdcc;
        padding: 0 1 1 1;
    }

    ModalScreen {
        align: center middle;
    }

    #dialog {
        width: 72;
        max-width: 95%;
        height: auto;
        max-height: 80%;
        background: #191f27;
        border: round #83a9d4;
        padding: 1 2;
    }

    .dialog-kicker {
        color: #8aa0b8;
        text-style: italic;
    }

    .dialog-title {
        text-style: bold;
        color: #f0f0f0;
        margin: 1 0;
    }

    .dialog-subject {
        color: #c8c8c8;
        margin-bottom: 1;
    }

    .dialog-meta {
        color: #c4a35a;
    }

    .dialog-reason {
        color: #9a9a9a;
        margin: 1 0;
    }

    .dialog-actions {
        height: auto;
        margin-top: 1;
    }

    .dialog-options {
        height: auto;
        margin: 1 0;
    }

    Tooltip {
        background: #253241;
        color: #e0e8f2;
        border: round #6682a1;
        padding: 1 2;
        max-width: 60;
    }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+x", "stop_turn", "Stop", priority=True),
        Binding("ctrl+e", "toggle_tools", "Toggle tools", show=False),
        Binding("ctrl+l", "scroll_end", "Scroll to bottom", show=False),
    ]

    def __init__(self, rt: WrightRuntime) -> None:
        super().__init__()
        self.rt = rt
        if not isinstance(rt.renderer, TUIRenderer):
            raise TypeError("TUI host requires TUIRenderer")
        self.renderer = rt.renderer
        self.session_thread: threading.Thread | None = None
        self._draft: AssistantBlock | None = None
        self._reasoning: ReasoningBlock | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._draining = False
        self._active_request: InteractionRequest | None = None
        self._stop = threading.Event()
        self._scroll_pending = False
        self._slash_completion = SlashCompletion()

    def compose(self) -> ComposeResult:
        with Horizontal(id="status"):
            yield Static("wright", id="brand")
            yield Static("", id="status-model")
            yield Static("", id="status-workspace")
            yield Static("● idle", id="status-state")
            yield Static("", id="status-meta")
            yield Static("○", id="context")
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer-wrap"):
            yield Static("", id="slash-suggestions")
            yield MultilineComposer(
                placeholder="Message Wright…",
                id="composer",
            )
            yield Static(
                "enter send  ·  shift+enter newline  ·  ctrl+x stop  ·  / commands",
                id="composer-hint",
            )

    def on_mount(self) -> None:
        self.renderer.attach(self)
        hub = self.renderer._hub
        if hub is not None:
            hub.bind_collector(interrupt=self.renderer.interrupt_main_prompt)
        self._seed_history()
        self._refresh_status()
        self.set_interval(0.25, self._refresh_status)
        self.session_thread = threading.Thread(
            target=self._session_loop,
            name="wright-session",
            daemon=True,
        )
        self.session_thread.start()
        self.query_one("#composer", MultilineComposer).focus()

    def on_unmount(self) -> None:
        self._stop.set()
        self.renderer.detach()
        self._fail_pending_interactions()
        try:
            self.rt.event_queue.put_nowait(("EXIT", None))
        except Exception:
            pass

    def _seed_history(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        if self.rt.resumed:
            sid = self.rt.session_state.session_id
            status = self.rt.session_state.status
            transcript.mount(SystemBlock(f"resumed {sid}  ({status})"))
        session = self.rt.session_state
        records = session.message_records
        positions = {record.id: index for index, record in enumerate(records)}
        shown_user = ""
        restored = False
        for turn in session.turns:
            assistant_index = positions.get(turn.message_id)
            if assistant_index is None:
                continue
            user_text = self._history_user_before(records, assistant_index)
            if user_text and user_text != shown_user:
                transcript.mount(UserBlock(user_text))
                shown_user = user_text
                restored = True

            assistant = records[assistant_index].message
            reasoning = assistant.get("reasoning_content") or turn.parsed.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                transcript.mount(ReasoningBlock(reasoning, collapsed=True))
                restored = True

            for call_id in turn.tool_execution_ids:
                execution = session.tool_executions.get(call_id)
                if execution is None:
                    continue
                result = execution.result.to_dict() if execution.result else None
                if result is None:
                    status = "running"
                else:
                    status = "done" if result.get("ok") else "error"
                transcript.mount(ToolBlock(ToolView(
                    key=call_id,
                    name=execution.call.name,
                    arguments=execution.call.arguments,
                    status=status,
                    result=result.get("data") if result and result.get("ok") else None,
                    error=str(result.get("err", "")) if result and not result.get("ok") else "",
                )))
                restored = True

            answer = turn.parsed.get("final_answer")
            if answer is not None:
                transcript.mount(AssistantBlock(_json_text(answer)))
                restored = True
            if turn.usage is not None:
                usage = turn.usage
                transcript.mount(UsageBlock(
                    f"{usage.prompt_tokens:,} in  ·  "
                    f"{usage.completion_tokens:,} out",
                ))
                restored = True
        if restored:
            if session.status != "running":
                usage = session.task_usage()
                if usage.total_tokens:
                    transcript.mount(TaskUsageBlock(
                        f"usage  Σ {_short_tokens(usage.total_tokens)}",
                        _task_usage_detail(
                            usage.prompt_tokens,
                            usage.completion_tokens,
                            usage.total_tokens,
                        ),
                    ))
            transcript.mount(SystemBlock("earlier turns above  ·  new messages follow"))
        self._scroll_to_end()

    @staticmethod
    def _history_user_before(records: list[Any], assistant_index: int) -> str:
        for record in reversed(records[:assistant_index]):
            message = record.message
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            stripped = content.lstrip()
            if stripped.startswith(("<system-reminder>", "<task-notification>")):
                continue
            if stripped.startswith("{") and any(
                key in stripped[:120]
                for key in ("tool_results", "verification_feedback")
            ):
                continue
            return content.strip()
        return ""

    def _session_loop(self) -> None:
        rt = self.rt
        try:
            if rt.resumed and rt.session_state.status == "running":
                rt.agent_idle.clear()
                try:
                    rt.agent.continue_run()
                finally:
                    rt.agent_idle.set()
            while not self._stop.is_set():
                event_type, payload = rt.event_queue.get()
                if process_session_event(rt, event_type, payload):
                    break
        except Exception:
            logger.exception("tui session loop failed")
            self.renderer.on_system_notice("session loop failed")
        if not self._stop.is_set():
            try:
                self.call_from_thread(self.exit)
            except RuntimeError:
                pass

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _close_draft(self, text: str | None = None) -> None:
        draft = self._draft
        if draft is None:
            return
        if text is not None:
            draft.set_final(text)
        else:
            draft.set_classes("msg assistant")
        self._draft = None

    def _ensure_draft(self) -> AssistantBlock:
        if self._draft is None:
            self._draft = AssistantBlock(draft=True)
            self._transcript().mount(self._draft)
            self._scroll_to_end()
        return self._draft

    def _ensure_reasoning(self) -> ReasoningBlock:
        if self._reasoning is None:
            self._reasoning = ReasoningBlock()
            self._transcript().mount(self._reasoning)
        return self._reasoning

    def _scroll_to_end(self) -> None:
        if self._scroll_pending:
            return
        self._scroll_pending = True
        self.call_after_refresh(self._scroll_to_end_after_refresh)

    def _scroll_to_end_after_refresh(self) -> None:
        self._scroll_pending = False
        self._transcript().scroll_end(animate=False, immediate=True)

    def on_turn_begin(self, _event: TurnBegin) -> None:
        self._close_draft()
        self._reasoning = None
        self._refresh_status()

    def on_stream_refresh(self, _event: StreamRefresh) -> None:
        reasoning, content = self.renderer.consume_stream()
        if reasoning:
            self._ensure_reasoning().update_reasoning(reasoning)
        if content:
            self._ensure_draft().set_draft(content)
            self._scroll_to_end()
        self._refresh_status()

    def on_draft_freeze(self, event: DraftFreeze) -> None:
        self._close_draft(event.text)
        self._scroll_to_end()

    def on_final_answer(self, event: FinalAnswer) -> None:
        if self._draft is None:
            block = AssistantBlock(event.text)
            self._transcript().mount(block)
        else:
            self._close_draft(event.text)
        self._scroll_to_end()
        self._refresh_status()

    def on_request_usage(self, event: RequestUsage) -> None:
        self._transcript().mount(UsageBlock(event.text))
        self._scroll_to_end()

    def on_task_usage(self, event: TaskUsage) -> None:
        self._transcript().mount(TaskUsageBlock(event.summary, event.detail))
        self._scroll_to_end()

    def on_tool_upsert(self, event: ToolUpsert) -> None:
        tool = event.tool
        existing = self._tools.get(tool.key)
        if existing is None:
            block = ToolBlock(tool)
            self._tools[tool.key] = block
            self._transcript().mount(block)
        else:
            existing.apply(tool)
        self._scroll_to_end()

    def on_system_notice(self, event: SystemNotice) -> None:
        self._transcript().mount(SystemBlock(event.text))
        self._scroll_to_end()

    def on_agent_event_notice(self, event: AgentEventNotice) -> None:
        self._transcript().mount(SubagentBlock(event.data, event.text))
        self._scroll_to_end()

    def on_status_changed(self, _event: StatusChanged) -> None:
        self._refresh_status()

    def on_interaction_needed(self, _event: InteractionNeeded) -> None:
        if self._draining:
            return
        self.run_worker(
            self._drain_interactions,
            exclusive=False,
            group="interaction",
            exit_on_error=False,
        )

    async def _drain_interactions(self) -> None:
        hub = self.renderer._hub
        if hub is None:
            return
        self._draining = True
        try:
            while True:
                request = hub.poll()
                if request is None:
                    break
                self._active_request = request
                try:
                    result = await self._collect(request)
                except Exception:
                    logger.exception("tui interaction failed")
                    result = None if request.kind == "ask_user" else "n"
                try:
                    request.reply.put_nowait(result)
                except Full:
                    pass
                self._active_request = None
        finally:
            self._draining = False
            self._active_request = None
            if hub.has_pending():
                self.run_worker(
                    self._drain_interactions,
                    exclusive=False,
                    group="interaction",
                    exit_on_error=False,
                )
            try:
                self.query_one("#composer", MultilineComposer).focus()
            except Exception:
                pass

    async def _collect(self, request: InteractionRequest) -> Any:
        payload = request.payload
        if request.kind == "permission":
            return await self.push_screen_wait(
                PermissionModal(
                    tool_name=str(payload.get("tool_name", "tool")),
                    subject=str(payload.get("subject", "")),
                    risk_flags=str(payload.get("risk_flags", "")),
                    reason=str(payload.get("reason", "")),
                    offer_always=bool(payload.get("offer_always")),
                    remember_rule=str(payload.get("remember_rule", "")),
                    remember_persists=bool(payload.get("remember_persists")),
                    revoke_hint=str(payload.get("revoke_hint", "")),
                )
            )
        if request.kind == "ask_user":
            options = payload.get("options") or ()
            return await self.push_screen_wait(
                AskUserModal(
                    question=str(payload.get("question", "")),
                    context=str(payload.get("context", "")),
                    options=tuple(options),
                )
            )
        return "n"

    def _fail_pending_interactions(self) -> None:
        if self._active_request is not None:
            _fail_interaction(self._active_request)
            self._active_request = None
        hub = self.renderer._hub
        if hub is None:
            return
        while True:
            request = hub.poll()
            if request is None:
                break
            _fail_interaction(request)

    def on_multiline_composer_submitted(self, event: MultilineComposer.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        if value in {"/exit", "/quit"}:
            self._request_quit()
            return
        if value == "/help":
            self.renderer.on_system_notice(tui_help_text())
            return
        if value == "/new":
            self._transition(SessionControlRequest.new())
            return
        if value == "/resume":
            self.run_worker(self._choose_resume(), group="session-control")
            return
        if value.startswith("/resume "):
            self._transition(SessionControlRequest.resume(value.removeprefix("/resume ").strip()))
            return
        if value == "/model":
            self.run_worker(self._choose_model(), group="session-control")
            return
        if value.startswith("/model "):
            self._set_model(value.removeprefix("/model ").strip())
            return
        if value == "/clear":
            self._transcript().remove_children()
            return
        self._transcript().mount(UserBlock(value))
        self._scroll_to_end()
        self.rt.event_queue.put(("USER_INPUT", value))
        self._refresh_status()

    def _control_available(self) -> bool:
        if self.rt.agent_idle.is_set():
            return True
        self.renderer.on_system_notice("session controls are available when Wright is idle")
        return False

    def _transition(self, request: SessionControlRequest) -> None:
        if not self._control_available():
            return
        store = self.rt.agent.checkpoint_store
        if store is not None:
            try:
                store.save(self.rt.session_state)
            except Exception as exc:
                self.renderer.on_system_notice(f"checkpoint failed: {exc}")
                return
        self._stop.set()
        try:
            self.rt.event_queue.put_nowait(("EXIT", None))
        except Exception:
            pass
        self.exit(result=request)

    async def _choose_resume(self) -> None:
        if not self._control_available():
            return
        sessions = self.rt.checkpoint_store.list_recent_sessions(limit=12)
        if not sessions:
            self.renderer.on_system_notice("no saved sessions")
            return
        session_id = await self.push_screen_wait(ResumeModal(sessions))
        if session_id:
            self._transition(SessionControlRequest.resume(session_id))

    async def _choose_model(self) -> None:
        if not self._control_available():
            return
        current = str(self.rt.llm.model)
        model = await self.push_screen_wait(ModelModal(available_models(current), current))
        if model:
            self._set_model(model)

    def _set_model(self, model: str) -> None:
        if not self._control_available() or not model:
            return
        current = str(self.rt.llm.model)
        if model == current:
            return
        self.rt.llm.model = model
        if self.rt.agent.llm is not self.rt.llm:
            self.rt.agent.llm.model = model
        previous_session_model = self.rt.session_state.model_name
        self.rt.session_state.model_name = model
        store = self.rt.agent.checkpoint_store
        if store is not None:
            try:
                store.save(self.rt.session_state)
            except Exception as exc:
                self.rt.llm.model = current
                if self.rt.agent.llm is not self.rt.llm:
                    self.rt.agent.llm.model = current
                self.rt.session_state.model_name = previous_session_model
                self.renderer.on_system_notice(f"checkpoint failed: {exc}")
                return
        self.renderer.on_system_notice(f"model  {current}  →  {model}")
        self._refresh_status()

    def on_multiline_composer_slash_changed(
        self, event: MultilineComposer.SlashChanged,
    ) -> None:
        self._slash_completion.update(event.text)
        self._render_slash_suggestions()

    def on_multiline_composer_slash_navigate(
        self, event: MultilineComposer.SlashNavigate,
    ) -> None:
        self._slash_completion.move(event.offset)
        self._render_slash_suggestions()

    def on_multiline_composer_slash_complete(
        self, _event: MultilineComposer.SlashComplete,
    ) -> None:
        command = self._slash_completion.selected
        if command is None:
            return
        composer = self.query_one("#composer", MultilineComposer)
        composer.load_text(f"{command.name} ")
        composer.focus()

    def on_multiline_composer_slash_dismissed(
        self, _event: MultilineComposer.SlashDismissed,
    ) -> None:
        self._slash_completion.update("")
        self._render_slash_suggestions()

    def _render_slash_suggestions(self) -> None:
        suggestions = self.query_one("#slash-suggestions", Static)
        matches = self._slash_completion.matches
        self.query_one("#composer", MultilineComposer).set_slash_menu_open(bool(matches))
        suggestions.display = bool(matches)
        if not matches:
            suggestions.update("")
            return
        lines = []
        for index, command in enumerate(matches):
            marker = "❯" if index == self._slash_completion.selected_index else " "
            lines.append(f"{marker} {command.name:<10} {command.description}")
        suggestions.update("\n".join(lines) + "\n  ↑↓ navigate  ·  tab complete  ·  esc close")

    def _request_quit(self) -> None:
        self._stop.set()
        try:
            self.rt.event_queue.put_nowait(("EXIT", None))
        except Exception:
            pass
        self.exit()

    async def action_quit(self) -> None:
        self._request_quit()

    def action_stop_turn(self) -> None:
        if self.rt.agent_idle.is_set():
            self.renderer.on_system_notice("no running task to stop")
            return
        self.rt.cancellation_event.set()
        self._fail_pending_interactions()
        screen = self.screen
        if isinstance(screen, PermissionModal):
            screen.dismiss("n")
        elif isinstance(screen, AskUserModal):
            screen.dismiss(None)
        self.renderer.on_system_notice("stopping current task…")
        self._refresh_status()

    def action_toggle_tools(self) -> None:
        tools = list(self.query(ToolBlock))
        if not tools:
            return
        any_collapsed = any(t.collapsed for t in tools)
        for t in tools:
            t.collapsed = not any_collapsed

    def action_scroll_end(self) -> None:
        self._scroll_to_end()

    def on_key(self, event: Key) -> None:
        focused = self.focused
        if focused is not None and getattr(focused, "id", None) == "transcript":
            if event.key in ("i", "enter"):
                event.prevent_default()
                event.stop()
                try:
                    self.query_one("#composer", MultilineComposer).focus()
                except Exception:
                    pass

    def _refresh_status(self) -> None:
        try:
            idle = self.rt.agent_idle.is_set()
            composer = self.query_one("#composer", MultilineComposer)
            composer.placeholder = (
                "Message Wright…" if idle else "Queue a follow-up…"
            )
            context_limit = getattr(self.rt.agent, "context_limit", None)
            context, tooltip, context_class = _context_ring(
                self.rt.session_state.context_tokens, context_limit,
            )
            plan = _plan_brief(self.rt.session_state)
            meta_parts = []
            if plan:
                meta_parts.append(plan)

            model = getattr(getattr(self.rt, "llm", None), "model", "") or ""
            if model:
                self.query_one("#status-model", Static).update(f"🤖 {model}")

            ws_dir = getattr(self.rt.session_state, "workspace_dir", None)
            ws_name = Path(ws_dir).name if ws_dir else Path.cwd().name
            self.query_one("#status-workspace", Static).update(f"📁 {ws_name}")

            state_icon = "●" if idle else "⏳"
            state_text = f"{state_icon} idle" if idle else f"{state_icon} running"
            status_state = self.query_one("#status-state", Static)
            status_state.update(state_text)
            status_state.set_class(not idle, "running")

            self.query_one("#status-meta", Static).update("  ·  ".join(meta_parts))
            indicator = self.query_one("#context", Static)
            indicator.update(context)
            indicator.tooltip = tooltip
            indicator.set_class(context_class == "warning", "warning")
        except Exception:
            pass


def _plan_brief(session_state: Any) -> str:
    manager = getattr(session_state, "plan_manager", None)
    if manager is None or not getattr(manager, "has_plan", False):
        return ""
    steps = getattr(manager, "steps", ())
    if not steps:
        return ""
    current = next((step for step in steps if step.status == "in_progress"), None)
    done = sum(1 for step in steps if step.status in {"completed", "skipped"})
    if current is not None:
        title = current.title
        if len(title) > 32:
            title = title[:29] + "…"
        return f"plan {done}/{len(steps)} {title}"
    return f"plan {getattr(manager, 'status', '')} {done}/{len(steps)}"


def run_tui(args: Any) -> None:
    require_interactive_tty()
    from ..interaction import InteractionHub

    renderer = TUIRenderer()
    renderer.bind_interaction(InteractionHub())
    active_args = args
    while True:
        rt = build_runtime(active_args, renderer=renderer)
        app = WrightTUI(rt)
        transition: SessionControlRequest | None = None
        try:
            result = app.run()
            if isinstance(result, SessionControlRequest):
                transition = result
            elif rt.agent.checkpoint_store:
                print(f"💾 会话已保存 (session_id: {rt.session_state.session_id})")
        finally:
            app._stop.set()
            try:
                rt.event_queue.put_nowait(("EXIT", None))
            except Exception:
                pass
            thread = app.session_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
            shutdown_runtime(rt)
        if transition is None:
            return
        active_args = runtime_args_for_transition(active_args, transition)
