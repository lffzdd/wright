"""One model request: project the context, stream the reply, report usage."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from ...core.logger import get_logger
from ...domain.gateway.llm_gateway import ILLMProvider
from ...domain.model.llm import ModelRequest, UsageRecord
from ...domain.model.llm.events import (
    ContentDelta,
    ContentDone,
    ReasoningDelta,
    UsageEvent,
)
from ...domain.model.llm.provider_usage import usage_from_provider
from ...domain.model.session import Session
from ...domain.protocol import encode_tools
from ..memory import MemoryManager
from ..session_runtime.events import SessionEvents
from .context import ContextBudgetExceeded, ContextBuilder
from .prompt import AgentPromptManager

logger = get_logger(__name__)


class ModelTurnRunner:
    def __init__(
        self,
        llm: ILLMProvider,
        session_state: Session,
        ui: SessionEvents,
        *,
        memory: MemoryManager | None,
        context_builder: ContextBuilder,
        prompt_manager: AgentPromptManager,
        active_deferred_tools: list[str],
        tool_schemas: list[dict],
        tool_names: dict[str, str],
        emit_lifecycle: Callable,
        is_cancelled: Callable[[], bool],
        record_auxiliary_usage: Callable[[UsageRecord], None],
    ) -> None:
        self.llm = llm
        self.session_state = session_state
        self.ui = ui
        self.memory = memory
        self.context_builder = context_builder
        self.compactor = context_builder.compactor
        self._prompt_manager = prompt_manager
        self._active_deferred_tools = active_deferred_tools
        self.tool_schemas = tool_schemas
        self._tool_names = tool_names
        self._emit_lifecycle = emit_lifecycle
        self._is_cancelled = is_cancelled
        self._record_auxiliary_usage = record_auxiliary_usage

    @property
    def context_limit(self) -> int | None:
        return self.llm.context_limit

    def run(
        self,
        reminders: Sequence[dict] = (),
    ) -> tuple[ContentDone, UsageRecord | None]:
        """渲染事件流，返回包含正文和工具调用的完整响应。

        ``reminders`` 只叠在本轮请求末尾，不写入 transcript。调用方先算
        token 再传入同一份列表，用量校准才能扣掉这笔临时开销。
        """

        # Schema exposure is snapshotted for this request. tool_search updates
        # the session set during tool execution, so new names apply next request.
        # Request compaction must not clear that set: folding is a projection.
        self.tool_schemas, self._tool_names = encode_tools(
            self._prompt_manager.visible_schema_tools(),
            active_deferred=set(self._active_deferred_tools),
        )
        self.ui.on_turn_begin()

        view, recall, reminders = self._build_turn_view(reminders)
        if view.omitted_optional_reminders:
            logger.info("episode_recall omitted reason=context_budget")
        self.session_state.request_context_tokens = view.estimated_tokens
        from ..workspace.context_usage import classify_context

        breakdown = classify_context(view)
        breakdown["limit"] = self.context_limit
        self.session_state.request_context_breakdown = breakdown
        self._emit_compaction_lifecycle(view)
        if view.over_budget:
            raise ContextBudgetExceeded(
                view.budget_reason
                or "context exceeds the request budget after deterministic compression"
            )
        note_injection = (
            getattr(self.memory, "note_injection", None)
            if self.memory is not None
            else None
        )
        if callable(note_injection):
            try:
                note_injection(recall, omitted=bool(view.omitted_optional_reminders))
            except Exception:
                logger.info("memory_meter failure_type=exception")
        wire_messages = view.messages

        self._emit_lifecycle("llm_start", {
            "model": str(getattr(self.llm, "model", "")),
            "message_count": len(wire_messages),
            "context_tokens": view.estimated_tokens,
            "has_plan_reminder": any(
                "<plan-state>" in str(message.get("content", ""))
                for message in reminders
            ),
        })
        started = time.monotonic()
        response, usage_record = self._stream_model_response(view, wire_messages, started)

        self.ui.on_usage(
            usage_record.prompt_tokens if usage_record else None,
            usage_record.completion_tokens if usage_record else None,
            usage_record.total_tokens if usage_record else None,
            self.context_limit,
            context_tokens=getattr(self.session_state, "request_context_tokens", None),
        )

        self._emit_lifecycle("llm_end", {
            "duration_ms": round((time.monotonic() - started) * 1_000, 3),
            "output_chars": len(response.content),
            "tool_call_count": len(response.tool_calls),
            "usage": (
                {
                    "prompt_tokens": usage_record.prompt_tokens,
                    "completion_tokens": usage_record.completion_tokens,
                    "total_tokens": usage_record.total_tokens,
                }
                if usage_record is not None else None
            ),
        })

        return response, usage_record

    def _build_turn_view(self, reminders: Sequence[dict]):
        """Project the transcript and this turn's reminders into one request view."""
        system_prompt = None
        if self.memory is not None:
            base_prompt = next(
                (
                    record.message["content"]
                    for record in self.session_state.message_records
                    if record.message.get("role") == "system"
                ),
                "",
            )
            system_prompt = self.memory.project_system_prompt(base_prompt)
        optional_reminders: list[dict] = []
        recall = None
        recall_for_turn = getattr(self.memory, "recall_for_turn", None) if self.memory is not None else None
        if callable(recall_for_turn):
            recall = recall_for_turn(self.session_state)
            reminders = list(reminders)
            if recall.semantic_text:
                reminders.append({"role": "user", "content": recall.semantic_text})
            if recall.episode_text:
                optional_reminders.append({"role": "user", "content": recall.episode_text})
        view = self.context_builder.build(
            self.session_state.message_records,
            tools=self.tool_schemas,
            reminders=reminders,
            optional_reminders=optional_reminders,
            system_prompt=system_prompt,
            context_limit=self.context_limit,
            conversation_key=self.session_state.session_id,
        )
        return view, recall, reminders

    def _emit_compaction_lifecycle(self, view) -> None:
        if view.folded_record_ids:
            # The projection folded older tool results. Activated tool schemas
            # belong to the session and stay available on later requests.
            self._emit_lifecycle("pre_compact", {
                "context_tokens": view.estimated_tokens,
                "context_limit": self.context_limit,
                "watermark": self.compactor.context_watermark,
            })
            self._emit_lifecycle("post_compact", {
                "folded_count": len(view.folded_record_ids),
                "token_savings": 0,
                "context_tokens": view.estimated_tokens,
                "deactivated_tools": 0,
                "folded_record_ids": list(view.folded_record_ids),
                "over_budget": view.over_budget,
            })

    def _stream_model_response(
        self,
        view,
        wire_messages,
        started: float,
    ) -> tuple[ContentDone, UsageRecord | None]:
        """Read one model stream, stopping early when the run is cancelled."""
        response = ContentDone("", finish_reason="incomplete")
        usage_record: UsageRecord | None = None
        self._partial_content: list[str] = []
        self._partial_reasoning: list[str] = []
        try:
            run_config = (
                self.session_state.active_run().model_config
                if self.session_state.active_run() is not None
                else {}
            )
            request = ModelRequest(
                messages=tuple(wire_messages),
                tools=tuple(view.tools),
                context_token_estimate=view.estimated_tokens,
                model=run_config.get("model") or None,
                transport=run_config.get("transport") or None,
            )
            stream = self.llm(request, tools=self.tool_schemas)
            for event in stream:
                # A complete response that already arrived is kept: the normal
                # cancellation path records it and closes its tool calls.
                if self._is_cancelled() and not isinstance(event, (ContentDone, UsageEvent)):
                    # Stop reading as soon as the person presses Stop. Closing
                    # the generator closes the provider stream underneath.
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                    response = ContentDone(
                        "".join(self._partial_content),
                        reasoning="".join(self._partial_reasoning),
                        finish_reason="cancelled",
                    )
                    break
                if isinstance(event, ReasoningDelta):
                    self._partial_reasoning.append(event.piece)
                    self.ui.on_reasoning_delta(event.piece)
                elif isinstance(event, ContentDelta):
                    self._partial_content.append(event.piece)
                    self.ui.on_content_delta(event.piece)
                elif isinstance(event, ContentDone):
                    response = event
                elif isinstance(event, UsageEvent):
                    usage_record = usage_from_provider(event.usage)

        except Exception as exc:
            if usage_record is not None:
                self._record_auxiliary_usage(usage_record)
            self._emit_lifecycle("llm_error", {
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.monotonic() - started) * 1_000, 3),
            })
            raise
        return response, usage_record
