"""Textual fullscreen host. Owns the event loop; Agent runs on a worker thread."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Static

from ...application.composition.runtime import WrightRuntime, assemble_runtime
from ...application.session.service import (
    SessionService,
    SessionServiceError,
    set_session_model,
)
from ...core.logger import get_logger
from ..cli.args import runtime_config_from_args
from ..cli.resume_select import choose_resume_session
from ..interaction import InteractionRequest
from .blocks import (
    AssistantBlock,
    ReasoningBlock,
    SubagentBlock,
    SystemBlock,
    TaskUsageBlock,
    ToolBlock,
    UsageBlock,
    UserBlock,
)
from .composer import MultilineComposer
from .format import _context_ring, _json_text, _short_tokens, _task_usage_detail
from .messages import (
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
    TurnBegin,
)
from .modals import AskUserModal, ModelModal, PermissionModal, ResumeModal
from .renderer import TUIRenderer
from .session_control import (
    SessionControlRequest,
    available_models,
    runtime_args_for_transition,
)
from .slash import SlashCompletion, tui_help_text
from .styles import APP_CSS
from .view_models import ToolView

logger = get_logger(__name__)


def require_interactive_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("TUI 需要交互式终端（stdin 与 stdout 均为 TTY）")

























class WrightTUI(App):
    """Fullscreen Wright session. Input stays pinned; Agent runs off-thread."""

    TITLE = "wright"
    ENABLE_COMMAND_PALETTE = False
    CSS = APP_CSS
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
        self.service: SessionService | None = (
            SessionService(rt) if hasattr(rt, "publisher") else None
        )
        self.session_thread: Any | None = None
        self._draft: AssistantBlock | None = None
        self._reasoning: ReasoningBlock | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._draining = False
        self._active_request: InteractionRequest | None = None
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
            yield Static("", id="attachments")
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
        hub = getattr(self.rt, "interaction_broker", None)
        if hub is not None and hasattr(hub, "bind_collector"):
            hub.bind_collector(interrupt=self._interrupt_for_interaction)
        self._seed_history()
        self._refresh_status()
        self.set_interval(0.25, self._refresh_status)
        if self.service is not None:
            self.service.start()
            self.session_thread = self.service.runner.thread
        self.query_one("#composer", MultilineComposer).focus()

    def on_unmount(self) -> None:
        self.renderer.detach()
        if self.service is not None:
            self.service.close(wait_timeout=0)

    def _seed_history(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        if self.rt.resumed:
            sid = self.rt.session_state.session_id
            status = self.rt.session_state.current_run_status()
            transcript.mount(SystemBlock(f"resumed {sid}  ({status})"))
        session = self.rt.session_state
        records = session.message_records
        positions = {record.id: index for index, record in enumerate(records)}
        shown_user: tuple[str, tuple[str, ...]] | None = None
        restored = False
        for turn in session.turns:
            assistant_index = positions.get(turn.message_id)
            if assistant_index is None:
                continue
            user_text, attachment_ids = self._history_user_before(records, assistant_index)
            user_key = (user_text, tuple(attachment_ids))
            if (user_text or attachment_ids) and user_key != shown_user:
                labels = [
                    f"[{index}] {record.filename} ({record.width}×{record.height})"
                    for index, attachment_id in enumerate(attachment_ids, 1)
                    if (record := session.attachments.get(attachment_id)) is not None
                ]
                transcript.mount(UserBlock(
                    user_text + ("\n🖼 " + "  ".join(labels) if labels else "")
                ))
                shown_user = user_key
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
            if session.current_run_status() != "running":
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
    def _history_user_before(records: list[Any], assistant_index: int) -> tuple[str, list[str]]:
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
            attachment_ids = message.get("attachments", [])
            return content.strip(), attachment_ids if isinstance(attachment_ids, list) else []
        return "", []

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

    def _interrupt_for_interaction(self) -> None:
        self.post_message(InteractionNeeded())

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
        hub = getattr(self.rt, "interaction_broker", None)
        if hub is None or not hasattr(hub, "poll"):
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
                    result = None if request.kind == "ask_user" else "deny"
                assert self.service is not None
                try:
                    self.service.respond_interaction(uuid4().hex, request.request_id, result)
                except SessionServiceError:
                    # Closing cancels the modal's request on its own control
                    # channel; a late UI answer is deliberately ignored.
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
                    risk_flags=tuple(payload.get("risk_flags") or ()),
                    reason=str(payload.get("reason", "")),
                    targets=tuple(payload.get("targets") or ()),
                    choices=tuple(payload.get("choices") or ()),
                    principal=str(payload.get("principal", "")),
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
        return "deny"

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
        try:
            assert self.service is not None
            self.service.submit(value)
        except SessionServiceError as exc:
            self.renderer.on_system_notice(str(exc))
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
        assert self.service is not None
        self.service.close(wait_timeout=0)
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
        try:
            if self.service is None:
                set_session_model(self.rt, model)
            else:
                self.service.set_model(model)
        except SessionServiceError as exc:
            self.renderer.on_system_notice(str(exc))
            return
        if model != current:
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
        if self.service is not None:
            self.service.close(wait_timeout=0)
        self.exit()

    async def action_quit(self) -> None:
        self._request_quit()

    def action_stop_turn(self) -> None:
        if self.rt.agent_idle.is_set():
            self.renderer.on_system_notice("no running task to stop")
            return
        assert self.service is not None
        self.service.cancel_current()
        screen = self.screen
        if isinstance(screen, PermissionModal):
            screen.dismiss("deny")
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
            attachment_bar = self.query_one("#attachments", Static)
            drafts = getattr(self.rt, "draft_attachments", None)
            pending = drafts.summaries() if drafts is not None else []
            if pending:
                attachment_bar.update("Attached: " + "  ".join(
                    f"[{index}] {record.filename} ({record.width}×{record.height})"
                    for index, record in enumerate(pending, 1)
                ))
                attachment_bar.display = True
            else:
                attachment_bar.display = False
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
    hub = InteractionHub()
    active_args = args
    # A TUI session is only a conversation owner.  Persisted automations use
    # an ApplicationHost which must outlive close/new/resume transitions in
    # this process, just as the Web RuntimeManager retains its hosts.
    hosts_by_session: dict[str, Any] = {}
    hosts_by_workspace: dict[str, Any] = {}
    try:
        while True:
            config = runtime_config_from_args(active_args)
            resume_id = getattr(active_args, "resume", None)
            host = hosts_by_session.get(resume_id) if resume_id else None
            if host is None and config.workspace is not None:
                host = hosts_by_workspace.get(str(config.workspace.resolve()))
            rt = assemble_runtime(
                config,
                renderer=renderer,
                interaction_broker=hub,
                application_host=host,
                resume_chooser=choose_resume_session,
            )
            if rt.application_host is not None:
                # The enclosing TUI is the application owner.  Closing this
                # particular conversation must not stop its automations.
                rt.owns_application_host = False
                hosts_by_session[rt.session_state.session_id] = rt.application_host
                hosts_by_workspace[str(rt.application_host.workspace_dir)] = rt.application_host
            app = WrightTUI(rt)
            transition: SessionControlRequest | None = None
            try:
                result = app.run()
                if isinstance(result, SessionControlRequest):
                    transition = result
                elif rt.agent.checkpoint_store:
                    print(f"💾 会话已保存 (session_id: {rt.session_state.session_id})")
            finally:
                stopped = app.service.close(wait_timeout=2.0) if app.service is not None else True
            if transition is not None and not stopped:
                print("会话仍在关闭中；尚未安全退出，无法切换会话。")
                return
            if transition is None:
                return
            active_args = runtime_args_for_transition(active_args, transition)
    finally:
        # Several session ids may reference the same local execution host.
        for host in set(hosts_by_session.values()) | set(hosts_by_workspace.values()):
            host.close()
