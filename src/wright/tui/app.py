"""Textual fullscreen host. Owns the event loop; Agent runs on a worker thread."""

from __future__ import annotations

import json
import sys
import threading
from queue import Full
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Static

from ..interaction import InteractionRequest
from ..logger import get_logger
from ..runtime import WrightRuntime, build_runtime, shutdown_runtime
from ..session_host import process_session_event
from .renderer import (
    DraftFreeze,
    FinalAnswer,
    InteractionNeeded,
    StatusChanged,
    StreamRefresh,
    SystemNotice,
    RequestUsage,
    TaskUsage,
    ToolUpsert,
    ToolView,
    TUIRenderer,
    TurnBegin,
)

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
        super().__init__(text, classes="msg user")


class AssistantBlock(Static):
    def __init__(self, text: str = "", *, draft: bool = False) -> None:
        classes = "msg assistant draft" if draft else "msg assistant"
        super().__init__(text or "…", classes=classes)

    def set_draft(self, text: str) -> None:
        self.set_classes("msg assistant draft")
        self.update(text or "…")

    def set_final(self, text: str) -> None:
        self.set_classes("msg assistant")
        self.update(text or "")


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
        body = Static(_tool_body(tool), classes="tool-body")
        super().__init__(
            body,
            title=_tool_title(tool),
            collapsed=True,
            classes=f"tool {_tool_class(tool)}",
        )
        self.tool_key = tool.key

    def apply(self, tool: ToolView) -> None:
        self.tool_key = tool.key
        self.title = _tool_title(tool)
        self.set_classes(f"tool {_tool_class(tool)}")
        try:
            body = self.query_one(".tool-body", Static)
            body.update(_tool_body(tool))
        except Exception:
            pass


def _tool_class(tool: ToolView) -> str:
    if tool.status == "error":
        return "error"
    if tool.status == "done":
        return "done"
    return "running"


def _tool_title(tool: ToolView) -> str:
    return f"{tool.name}  ·  {tool.status}"


def _tool_body(tool: ToolView) -> str:
    parts: list[str] = []
    if tool.arguments:
        parts.append(_json_text(tool.arguments))
    if tool.output:
        lines = tool.output.splitlines()
        clipped = lines[-_COMMAND_OUTPUT_LINES:]
        prefix = "" if len(lines) <= _COMMAND_OUTPUT_LINES else "…\n"
        parts.append(prefix + "\n".join(clipped))
    if tool.status == "error" and tool.error:
        parts.append(tool.error)
    elif tool.result is not None:
        parts.append(_json_text(tool.result))
    return "\n\n".join(part for part in parts if part) or "(no payload)"


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
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.subject = subject
        self.risk_flags = risk_flags
        self.reason = reason
        self.offer_always = offer_always

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("permission", classes="dialog-kicker")
            yield Static(self.tool_name, classes="dialog-title")
            if self.subject:
                yield Static(self.subject, classes="dialog-subject")
            yield Static(f"risk  {self.risk_flags}", classes="dialog-meta")
            yield Static(self.reason, classes="dialog-reason")
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

    #status-state {
        width: auto;
        padding: 0 2;
        color: #94b9ae;
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="status"):
            yield Static("wright", id="brand")
            yield Static("idle", id="status-state")
            yield Static("", id="status-meta")
            yield Static("○", id="context")
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer-wrap"):
            yield Input(
                placeholder="Message Wright…",
                id="composer",
            )
            yield Static("enter send  ·  ctrl+q quit", id="composer-hint")

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
        self.query_one("#composer", Input).focus()

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
                self.query_one("#composer", Input).focus()
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "composer":
            return
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        if value in {"/exit", "/quit"}:
            self.action_quit()
            return
        self._transcript().mount(UserBlock(value))
        self._scroll_to_end()
        self.rt.event_queue.put(("USER_INPUT", value))
        self._refresh_status()

    def action_quit(self) -> None:
        self._stop.set()
        try:
            self.rt.event_queue.put_nowait(("EXIT", None))
        except Exception:
            pass
        self.exit()

    def _refresh_status(self) -> None:
        try:
            idle = self.rt.agent_idle.is_set()
            state = "idle" if idle else "running"
            composer = self.query_one("#composer", Input)
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
            self.query_one("#status-state", Static).update(state)
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
    rt = build_runtime(args, renderer=renderer)
    app = WrightTUI(rt)
    try:
        app.run()
        if rt.agent.checkpoint_store:
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
