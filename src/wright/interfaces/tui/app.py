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
from ..cli.resume_select import choose_resume_session
from ..i18n import language_label, set_locale, t
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
from .session_control import available_models
from .slash import SlashCompletion, tui_help_text
from .styles import APP_CSS
from .view_models import ToolView

logger = get_logger(__name__)


def require_interactive_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(t("cli.tui_tty_required"))

























class WrightTUI(App):
    """Fullscreen Wright session. Input stays pinned; Agent runs off-thread."""

    TITLE = "wright"
    ENABLE_COMMAND_PALETTE = False
    CSS = APP_CSS
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+x", "stop_turn", "Stop", priority=True),
        Binding("ctrl+shift+x", "stop_all", "Stop all", priority=True),
        Binding("ctrl+e", "toggle_tools", "Toggle tools", show=False),
        Binding("ctrl+l", "scroll_end", "Scroll to bottom", show=False),
    ]

    def __init__(
        self,
        rt: WrightRuntime,
        *,
        renderer: TUIRenderer | None = None,
        service: SessionService | None = None,
        directory: Any = None,
        listener_id: str | None = None,
    ) -> None:
        super().__init__()
        self.rt = rt
        self.directory = directory
        self._listener_id = listener_id
        selected = renderer if renderer is not None else getattr(rt, "renderer", None)
        if not isinstance(selected, TUIRenderer):
            raise TypeError("TUI host requires TUIRenderer")
        self.renderer = selected
        if service is not None:
            self.service: SessionService | None = service
        else:
            self.service = SessionService(rt) if hasattr(rt, "publisher") else None
        self.session_thread: Any | None = None
        self._draft: AssistantBlock | None = None
        self._reasoning: ReasoningBlock | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._draining = False
        self._active_request: InteractionRequest | None = None
        self._scroll_pending = False
        self._slash_completion = SlashCompletion()
        self._mentions: list[dict[str, Any]] = []
        self._mention_index = 0

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
            yield Static(t("tui.hint"), id="composer-hint")

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
        if self.directory is None and self.service is not None:
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
        shown_turn: str | None = None
        restored = False
        history = self.service.history() if self.service is not None else []
        if history:
            for item in history:
                turn_id = str(item.get("turn_id") or "")
                user_text = str(item.get("user") or "")
                attachment_ids = [
                    str(attachment.get("id"))
                    for attachment in item.get("attachments") or []
                    if isinstance(attachment, dict) and attachment.get("id")
                ]
                if (user_text or attachment_ids) and turn_id != shown_turn:
                    labels = [
                        f"[{index}] {record.filename} ({record.width}×{record.height})"
                        for index, attachment_id in enumerate(attachment_ids, 1)
                        if (record := session.attachments.get(attachment_id)) is not None
                    ]
                    transcript.mount(UserBlock(
                        user_text + ("\n🖼 " + "  ".join(labels) if labels else "")
                    ))
                    shown_turn = turn_id
                    restored = True
                if item.get("assistant"):
                    transcript.mount(AssistantBlock(str(item["assistant"])))
                    restored = True
                for tool in item.get("tools") or []:
                    if not isinstance(tool, dict):
                        continue
                    ok = tool.get("ok")
                    transcript.mount(ToolBlock(ToolView(
                        key=str(tool.get("call_id") or ""),
                        name=str(tool.get("name") or "tool"),
                        arguments=tool.get("arguments") if isinstance(tool.get("arguments"), dict) else {},
                        status="done" if ok else "error" if ok is False else str(tool.get("phase") or "done"),
                        result=tool.get("data") if ok else None,
                        error=str(tool.get("err") or ""),
                    )))
                    restored = True
            if restored:
                transcript.mount(SystemBlock("earlier turns above  ·  new messages follow"))
            self._scroll_to_end()
            return
        for turn in session.turns:
            assistant_index = positions.get(turn.message_id)
            if assistant_index is None:
                continue
            user_text, attachment_ids = self._history_user_before(records, assistant_index)
            user_key = str(getattr(turn, "step_id", "") or id(turn))
            if (user_text or attachment_ids) and user_key != shown_turn:
                labels = [
                    f"[{index}] {record.filename} ({record.width}×{record.height})"
                    for index, attachment_id in enumerate(attachment_ids, 1)
                    if (record := session.attachments.get(attachment_id)) is not None
                ]
                transcript.mount(UserBlock(
                    user_text + ("\n🖼 " + "  ".join(labels) if labels else "")
                ))
                shown_turn = user_key
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
                    operation=str(payload.get("operation", "")),
                    grant_summary=str(payload.get("grant_summary", "")),
                    preview=str(payload.get("preview", "")),
                    cwd=str(payload.get("cwd", "")),
                    command=str(payload.get("command", "")),
                    http_method=str(payload.get("http_method", "")),
                    http_target=str(payload.get("http_target", "")),
                    shell_note=str(payload.get("shell_note", "")),
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
        from ...application.session.controls import apply_control, interpret_control

        action = interpret_control(value)
        if action is not None and action.name == "new":
            self._open_other(environment=action.argument or "local")
            return
        if action is not None and action.name == "resume":
            if action.argument:
                self._open_other(resume=action.argument)
            else:
                self.run_worker(self._choose_resume(), group="session-control")
            return
        if action is not None and action.name == "close":
            self._close_viewed()
            return
        if action is not None and action.name == "exit":
            self._request_quit()
            return
        if action is not None and action.name != "exit":
            try:
                assert self.service is not None
                result = apply_control(self.service, action)
            except (SessionServiceError, ValueError, OSError) as exc:
                self.renderer.on_system_notice(str(exc))
                return
            if action.name == "status":
                session = result.get("session", {})
                subs = result.get("subagents") or []
                self.renderer.on_system_notice(
                    "  ".join(
                        part for part in (
                            str(session.get("execution") or ""),
                            str(session.get("queue_reason") or ""),
                            str(session.get("environment") or ""),
                            str(session.get("execution_root") or ""),
                            f"subagents={len(subs)}" if subs else "",
                        ) if part
                    )
                )
            elif action.name == "model" and action.argument:
                self.renderer.on_system_notice(t("tui.model_changed", current="", model=action.argument))
            self._refresh_status()
            return
        if value == "/model":
            self.run_worker(self._choose_model(), group="session-control")
            return
        if value.startswith("/model "):
            self._set_model(value.removeprefix("/model ").strip())
            return
        if value == "/language" or value.startswith("/language "):
            self._set_language(value.removeprefix("/language").strip())
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

    def _set_language(self, raw: str) -> None:
        """Change interface copy without rebuilding the agent or clearing the session."""
        if not raw:
            self.renderer.on_system_notice(
                t("tui.language.current", language=language_label())
            )
            self._refresh_status()
            return
        if raw not in {"en", "zh-CN"}:
            self.renderer.on_system_notice(t("tui.language.invalid"))
            return
        set_locale(raw, persist=True)
        self.renderer.on_system_notice(
            t("tui.language.updated", language=language_label(raw))
        )
        self._refresh_status()
        try:
            for block in self.query(ToolBlock):
                tool = getattr(block, "_tool", None)
                if tool is not None:
                    block.apply(tool)
            for block in self.query(ReasoningBlock):
                refresh = getattr(block, "refresh_language", None)
                if callable(refresh):
                    refresh()
        except Exception:
            pass

    def _open_other(self, *, environment: str | None = None, resume: str | None = None) -> None:
        """Open or resume a session and show it. The previous session stays open."""

        directory = self.directory
        if directory is None or self.service is None:
            self.renderer.on_system_notice(t("tui.no_sessions"))
            return
        from ..interaction import InteractionHub

        try:
            opened = directory.open(
                environment=environment,
                resume=resume,
                model=str(self.service.summary().get("model") or "") or None,
                interaction_broker=InteractionHub(),
            )
        except Exception as exc:
            self.renderer.on_system_notice(str(exc))
            return
        self._show(opened)
        self.renderer.on_system_notice(
            f"{opened.session_id}  {opened.runtime.session_state.environment}"
        )

    def _show(self, opened: Any) -> None:
        previous = self.rt
        publisher = getattr(previous, "publisher", None)
        if self._listener_id and publisher is not None and hasattr(publisher, "remove_listener"):
            publisher.remove_listener(self._listener_id)
            self._listener_id = None
        hub = getattr(previous, "interaction_broker", None)
        if hub is not None and hasattr(hub, "bind_collector"):
            hub.bind_collector(interrupt=lambda: None)
        self.rt = opened.runtime
        self.service = opened.service
        from ..rendering.attach import attach_renderer

        self._listener_id = attach_renderer(
            opened.publisher, self.renderer, session=opened.runtime.session_state,
        )
        viewed = getattr(self.rt, "interaction_broker", None)
        if viewed is not None and hasattr(viewed, "bind_collector"):
            viewed.bind_collector(interrupt=self._interrupt_for_interaction)
        try:
            self.query_one("#transcript", VerticalScroll).remove_children()
        except Exception:
            pass
        self._draft = None
        self._reasoning = None
        self._tools = {}
        self._seed_history()
        self._refresh_status()
        if viewed is not None and getattr(viewed, "has_pending", lambda: False)():
            self.post_message(InteractionNeeded())

    def _close_viewed(self) -> None:
        directory = self.directory
        if directory is None or self.service is None:
            self._request_quit()
            return
        current = self.service.session_id
        directory.close(current)
        self.renderer.on_system_notice(t("tui.session_closed", session_id=current))
        active = [
            item for item in directory.list_sessions()
            if item.get("active") and item.get("session_id") != current
        ]
        if active:
            self._show(directory.get(str(active[0]["session_id"])))
            return
        self._open_other(environment="local")

    async def _choose_resume(self) -> None:
        if self.service is None:
            self.renderer.on_system_notice(t("tui.no_sessions"))
            return
        sessions = self.service.list_saved_sessions(limit=12)
        if not sessions:
            self.renderer.on_system_notice(t("tui.no_sessions"))
            return
        session_id = await self.push_screen_wait(ResumeModal(sessions))
        if session_id:
            self._open_other(resume=str(session_id))

    async def _choose_model(self) -> None:
        if not self._model_change_available():
            return
        current = str(self.service.summary()["model"]) if self.service is not None else str(self.rt.llm.model)
        model = await self.push_screen_wait(ModelModal(available_models(current), current))
        if model:
            self._set_model(model)

    def _model_change_available(self) -> bool:
        if self.service is not None and self.service.summary().get("execution") != "idle":
            self.renderer.on_system_notice(t("tui.controls_idle"))
            return False
        return True

    def _set_model(self, model: str) -> None:
        if not self._model_change_available() or not model:
            return
        current = (
            str(self.service.summary()["model"])
            if self.service is not None else str(self.rt.llm.model)
        )
        try:
            if self.service is None:
                set_session_model(self.rt, model)
            else:
                self.service.set_model(model)
        except SessionServiceError as exc:
            self.renderer.on_system_notice(str(exc))
            return
        if model != current:
            self.renderer.on_system_notice(t("tui.model_changed", current=current, model=model))
        self._refresh_status()

    def on_multiline_composer_slash_changed(
        self, event: MultilineComposer.SlashChanged,
    ) -> None:
        from ...application.workspace.references import active_mention_query

        query = active_mention_query(event.text)
        directory = self.directory
        if query is not None and directory is not None:
            self._mentions = list(directory.search_references(query))[:8]
            self._mention_index = min(self._mention_index, max(0, len(self._mentions) - 1))
            self._slash_completion.update("")
            self._render_mention_suggestions()
            return
        self._mentions = []
        self._slash_completion.update(event.text)
        self._render_slash_suggestions()

    def on_multiline_composer_slash_navigate(
        self, event: MultilineComposer.SlashNavigate,
    ) -> None:
        if self._mentions:
            self._mention_index = (self._mention_index + event.offset) % len(self._mentions)
            self._render_mention_suggestions()
            return
        self._slash_completion.move(event.offset)
        self._render_slash_suggestions()

    def on_multiline_composer_slash_complete(
        self, _event: MultilineComposer.SlashComplete,
    ) -> None:
        if self._mentions and self.service is not None:
            chosen = self._mentions[self._mention_index]
            try:
                self.service.stage_reference(str(chosen.get("path") or ""))
            except SessionServiceError as exc:
                self.renderer.on_system_notice(str(exc))
                return
            composer = self.query_one("#composer", MultilineComposer)
            from ...application.workspace.references import active_mention_query

            text = composer.text
            query = active_mention_query(text) or ""
            trimmed = text[: len(text) - len(query) - 1].rstrip()
            composer.load_text(trimmed)
            self._mentions = []
            self._render_slash_suggestions()
            self._refresh_status()
            composer.focus()
            return
        command = self._slash_completion.selected
        if command is None:
            return
        composer = self.query_one("#composer", MultilineComposer)
        composer.load_text(f"{command.name} ")
        composer.focus()

    def on_multiline_composer_slash_dismissed(
        self, _event: MultilineComposer.SlashDismissed,
    ) -> None:
        self._mentions = []
        self._slash_completion.update("")
        self._render_slash_suggestions()

    def _render_mention_suggestions(self) -> None:
        suggestions = self.query_one("#slash-suggestions", Static)
        composer = self.query_one("#composer", MultilineComposer)
        composer.set_slash_menu_open(bool(self._mentions))
        suggestions.display = bool(self._mentions)
        if not self._mentions:
            suggestions.update("")
            return
        lines = []
        for index, item in enumerate(self._mentions):
            marker = "❯" if index == self._mention_index else " "
            lines.append(f"{marker} {item.get('path')}")
        suggestions.update("\n".join(lines))

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
        suggestions.update("\n".join(lines) + "\n  " + t("tui.slash_hint"))

    def _request_quit(self) -> None:
        if self.directory is None and self.service is not None:
            self.service.close(wait_timeout=0)
        self.exit()

    async def action_quit(self) -> None:
        self._request_quit()

    def action_stop_turn(self) -> None:
        if self.rt.agent_idle.is_set():
            self.renderer.on_system_notice(t("tui.nothing_to_stop"))
            return
        assert self.service is not None
        self.service.stop_current()
        screen = self.screen
        if isinstance(screen, PermissionModal):
            screen.dismiss("deny")
        elif isinstance(screen, AskUserModal):
            screen.dismiss(None)
        self.renderer.on_system_notice(t("tui.stopping"))
        self._refresh_status()

    def action_stop_all(self) -> None:
        if self.service is None:
            return
        self.service.stop_all()
        self.renderer.on_system_notice(t("tui.stopping_all"))
        self._refresh_status()

    def action_toggle_tools(self) -> None:
        tools = list(self.query(ToolBlock))
        if not tools:
            return
        any_collapsed = any(t.collapsed for t in tools)
        for tool in tools:
            tool.collapsed = not any_collapsed

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
            if self.service is not None:
                view = self.service.view()
                execution = str(view.get("execution") or "idle")
                idle = execution == "idle"
                context_tokens = int(view.get("context_tokens") or 0)
                context_limit = view.get("context_limit")
                plan = str(view.get("plan_brief") or "")
                model = str(view.get("model") or "")
                ws_dir = view.get("workspace_dir")
                pending = view.get("draft_attachments") or []
                staged = view.get("staged_references") or []
                queue_reason = str(view.get("queue_reason") or "")
            else:
                idle_event = getattr(self.rt, "agent_idle", None)
                idle = idle_event is None or idle_event.is_set()
                execution = "idle" if idle else "running"
                context_tokens = int(getattr(self.rt.session_state, "context_tokens", 0) or 0)
                context_limit = getattr(getattr(self.rt, "agent", None), "context_limit", None)
                plan = _plan_brief(self.rt.session_state)
                model = str(getattr(getattr(self.rt, "llm", None), "model", "") or "")
                ws_dir = getattr(self.rt.session_state, "workspace_dir", None)
                drafts = getattr(self.rt, "draft_attachments", None)
                pending = drafts.summaries() if drafts is not None else []
                staged = []
                queue_reason = ""
            composer = self.query_one("#composer", MultilineComposer)
            composer.placeholder = t("tui.placeholder_idle") if idle else t("tui.placeholder_busy")
            self.query_one("#composer-hint", Static).update(t("tui.hint"))
            context, tooltip, context_class = _context_ring(context_tokens, context_limit)
            meta_parts = []
            if plan:
                meta_parts.append(plan)
            if execution == "queued" and queue_reason:
                meta_parts.append(queue_reason)
            elif execution == "waiting_for_input":
                meta_parts.append(t("tui.status.waiting"))
            if model:
                self.query_one("#status-model", Static).update(f"🤖 {model}")
            ws_name = Path(ws_dir).name if ws_dir else Path.cwd().name
            self.query_one("#status-workspace", Static).update(f"📁 {ws_name}")
            labels = {
                "idle": "● " + t("tui.status.idle"),
                "running": "⏳ " + t("tui.status.running"),
                "queued": "⏳ " + t("tui.status.queued"),
                "waiting_for_input": "⏳ " + t("tui.status.waiting_input"),
                "cancelling": "⏳ " + t("tui.status.cancelling"),
                "closing": "○ " + t("tui.status.closing"),
                "closed": "○ " + t("tui.status.closed"),
            }
            status_state = self.query_one("#status-state", Static)
            status_state.update(labels.get(execution, f"⏳ {execution}"))
            status_state.set_class(execution not in {"idle", "closed"}, "running")
            self.query_one("#status-meta", Static).update("  ·  ".join(meta_parts))
            attachment_bar = self.query_one("#attachments", Static)
            if pending:
                def _attached(record: Any) -> tuple[Any, Any, Any]:
                    if isinstance(record, dict):
                        return record.get("filename"), record.get("width"), record.get("height")
                    return record.filename, record.width, record.height

                listing = "  ".join(
                    f"[{index}] {filename} ({width}×{height})"
                    for index, record in enumerate(pending, 1)
                    for filename, width, height in [_attached(record)]
                )
            else:
                listing = ""
            ref_listing = "  ".join(
                f"@{item.get('path')}" for item in staged if isinstance(item, dict) and item.get("path")
            )
            shown = "  ".join(part for part in (listing, ref_listing) if part)
            if shown:
                attachment_bar.update(shown)
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
    from ...application.session.directory import SessionDirectory, SessionDirectoryError
    from ..interaction import InteractionHub
    from ..rendering.attach import attach_renderer

    workspace = Path(getattr(args, "workspace", None) or Path.cwd()).expanduser().resolve()
    directory = SessionDirectory(
        workspace,
        capacity=int(getattr(args, "web_capacity", 4) or 4),
        base_args=args,
        assemble=assemble_runtime,
    )
    renderer = TUIRenderer()
    hub = InteractionHub()
    try:
        opened = directory.open(
            environment=getattr(args, "environment", None),
            model=getattr(args, "model", None),
            resume=getattr(args, "resume", None),
            continue_latest=bool(getattr(args, "continue_latest", False)),
            interaction_broker=hub,
            resume_chooser=choose_resume_session,
        )
    except SessionDirectoryError as exc:
        directory.shutdown()
        raise SystemExit(str(exc)) from exc
    listener_id = attach_renderer(
        opened.publisher, renderer, session=opened.runtime.session_state,
    )
    app = WrightTUI(
        opened.runtime,
        renderer=renderer,
        service=opened.service,
        directory=directory,
        listener_id=listener_id,
    )
    try:
        app.run()
    finally:
        directory.shutdown()
