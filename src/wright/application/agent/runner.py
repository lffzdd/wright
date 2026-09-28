import json
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, ClassVar

from ...core.logger import get_logger
from ...domain.model.llm import ModelRequest, UsageRecord
from ...domain.model.llm.events import (
    ContentDelta,
    ContentDone,
    ReasoningDelta,
    UsageEvent,
)
from ...domain.model.session import Session
from ...domain.policy import AuthorizationChange
from ...domain.policy.verifier import Verifier
from ...domain.protocol import encode_tools
from ...infrastructure.llm.llm import LLMClient
from ...infrastructure.llm.usage import usage_from_provider
from ...infrastructure.storage.attachments import (
    MAX_ATTACHMENTS_PER_TURN,
    MAX_TOTAL_ATTACHMENT_BYTES,
)
from ...utils.token_counter import estimate_message_tokens
from ..composition.services import RuntimeServices
from ..memory import MemoryManager
from ..session.events import SessionEvents
from ..session.live_resources import RuntimeResources
from ..skills import SkillRegistry
from ..tool_execution.capabilities import (
    CapabilityAssembly,
    assemble_tool_capabilities,
)
from .components import AgentComponents, PreparedTools
from .context import ContextBudgetExceeded
from .parse import TurnAbort, parse_turn
from .prompt import AgentPromptManager
from .turns import (
    RetryCounters,
    TurnControl,
    handle_final_turn,
    handle_invalid_turn,
    handle_tool_calls_turn,
)
from .usage import AgentUsageTracker

if TYPE_CHECKING:
    from ...infrastructure.persistence.session.repository import FileSessionRepository

logger = get_logger(__name__)


class Agent:
    _TERMINAL_MARKERS: ClassVar[dict[str, str]] = {
        "failed": "mark_failed",
        "max_steps": "mark_max_steps",
    }
    def __init__(
        self,
        llm: LLMClient,
        session_state: Session,
        events: SessionEvents,
        prepared: PreparedTools,
        assembly: CapabilityAssembly,
        *,
        components: AgentComponents,
        max_consecutive_invalid: int = 3,
        memory: MemoryManager | None = None,
        verifier: Verifier | None = None,
        max_verification_retries: int = 3,
        checkpoint_store: "FileSessionRepository | None" = None,
        usage_observer: Callable[[UsageRecord], None] | None = None,
        lifecycle=None,
        skills: SkillRegistry | None = None,
        services: RuntimeServices | None = None,
        runtime_resources: RuntimeResources | None = None,
        execution_journal=None,
        execution_journal_factory=None,
        on_run_started: Callable[[str], None] | None = None,
        authorization_commit: Callable[[AuthorizationChange], None] | None = None,
        authorization_commit_factory=None,
        expose_autonomy: bool = False,
    ):
        self.llm = llm
        self.session_state = session_state
        self.ui = events
        self.services = services
        self._execution_journal_factory = execution_journal_factory
        self._authorization_commit_factory = authorization_commit_factory
        self._authorization_commit = authorization_commit
        self.runtime_resources = assembly.runtime_resources or runtime_resources
        if self.runtime_resources is None:
            self.runtime_resources = RuntimeResources(session_state.session_id)
        self._expose_autonomy = expose_autonomy
        # 长期记忆协作者:只主 Agent 注入,子 Agent 传 None(保持纯净隔离上下文)。
        # Agent 只在主循环里喊它三声:构造时取指令、每轮注入召回、收口后提取落盘。
        self.memory = memory
        self.skills = skills
        self.verifier = verifier
        self.max_verification_retries = max_verification_retries
        self.checkpoint_store = checkpoint_store
        self.last_checkpoint_error: Exception | None = None
        self._usage_observer = usage_observer
        self._execution_journal = execution_journal
        if self.memory is not None:
            self.memory.usage_observer = self._record_auxiliary_usage
            bind_project = getattr(self.memory, "bind_project", None)
            if callable(bind_project):
                bind_project(self.session_state.project_root)
            self._recover_pending_episodes()
        self.lifecycle = lifecycle
        self._memory_finalized_turns: set[str] = set()
        if max_verification_retries < 1:
            raise ValueError("max_verification_retries 必须 >= 1")
        self.on_run_started = on_run_started
        self.max_consecutive_invalid = max_consecutive_invalid
        self.profile = prepared.profile
        self.capabilities = prepared.capabilities
        self._active_deferred_tools = session_state.active_deferred_tools
        self._schema_tools = prepared.tools
        self.tool_schemas = prepared.schemas
        self._tool_names = prepared.names

        self.cancellation = components.cancellation
        self.context_builder = components.context_builder
        self.compactor = self.context_builder.compactor
        self.tool_dispatcher = components.tool_dispatcher
        self.executor = self.tool_dispatcher

        self._usage_tracker = AgentUsageTracker(
            session_state,
            events,
            usage_observer=usage_observer,
            has_live_agent_tasks=self._has_live_agent_tasks,
        )
        self._prompt_manager = AgentPromptManager(
            session_state,
            skills=skills,
            schema_tools=self._schema_tools,
        )

    @property
    def context_limit(self) -> int | None:
        return self.llm.context_limit

    @property
    def messages(self) -> Sequence[dict]:
        return self.session_state.messages

    def _emit_lifecycle(self, event: str, payload: dict):
        if self.lifecycle is None:
            return None
        return self.lifecycle.emit(
            event,
            payload,
            agent_task_id=self.session_state.agent_task_id,
            root_turn_id=self.session_state.agent_root_turn_id,
        )

    def _record_run_event(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_key: str = "",
    ) -> None:
        """Persist one durable history fact before the run advances.

        Interactive sessions have no journal.  Durable hosts provide the
        narrow journal object.  A history write failure is a run failure, not
        a reason to keep executing with an unverifiable state transition.
        """
        journal = self._execution_journal
        recorder = getattr(journal, "record_event", None) if journal is not None else None
        if not callable(recorder):
            return
        try:
            recorder(event_type, payload or {}, event_key=event_key)
        except Exception as exc:
            logger.error("durable Run history write failed", exc_info=True)
            raise RuntimeError(
                f"durable Run history persistence failed for {event_type}"
            ) from exc

    def _emit_agent_start(
        self,
        prompt: str,
        *,
        resumed: bool = False,
        source: str = "user",
    ) -> None:
        event = (
            "subagent_start"
            if self.session_state.agent_task_id is not None
            else "agent_start"
        )
        self._emit_lifecycle(event, {
            "prompt": prompt,
            "resumed": resumed,
            "source": source,
            "max_steps": self.session_state.max_steps,
        })

    def _emit_agent_stop(
        self,
        status: str,
        *,
        final_answer: str | None = None,
        reason: str = "",
    ):
        event = (
            "subagent_stop"
            if self.session_state.agent_task_id is not None
            else "agent_stop"
        )
        return self._emit_lifecycle(event, {
            "status": status,
            "final_answer": final_answer or "",
            "reason": reason,
            "steps": self.session_state.step_count,
            "usage": {
                "prompt_tokens": self.session_state.total_usage.prompt_tokens,
                "completion_tokens": self.session_state.total_usage.completion_tokens,
                "total_tokens": self.session_state.total_usage.total_tokens,
            },
        })

    def _run_turn(
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
        response = ContentDone("", finish_reason="incomplete")
        usage_record: UsageRecord | None = None
        self.ui.on_turn_begin()

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
        )
        if view.omitted_optional_reminders:
            logger.info("episode_recall omitted reason=context_budget")
        self.session_state.request_context_tokens = view.estimated_tokens
        from ..workspace.context_usage import classify_context

        breakdown = classify_context(view)
        breakdown["limit"] = self.context_limit
        self.session_state.request_context_breakdown = breakdown
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
            for event in self.llm(request, tools=self.tool_schemas):
                if isinstance(event, ReasoningDelta):
                    self.ui.on_reasoning_delta(event.piece)
                elif isinstance(event, ContentDelta):
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

    def _plan_reminder(self) -> dict | None:
        return self._prompt_manager.plan_reminder()

    def _ephemeral_reminders(self) -> list[dict]:
        return self._prompt_manager.ephemeral_reminders()

    def _record_auxiliary_usage(self, usage: UsageRecord) -> None:
        self._usage_tracker.record_auxiliary_usage(usage)

    def _render_usage_summary(self) -> None:
        self._usage_tracker.render_usage_summary()

    def _record_usage_for_turn(
        self,
        turn_record,
        usage_record: UsageRecord,
        transient_plan_tokens: int,
    ) -> None:
        self._usage_tracker.record_usage_for_turn(
            turn_record, usage_record, transient_plan_tokens
        )

    def _notify_usage(self, usage_record: UsageRecord) -> None:
        self._usage_tracker.notify_usage(usage_record)

    def _bind_executor_run(self) -> None:
        """Reassemble tool capabilities after the active Run is known."""
        if self._authorization_commit_factory is not None:
            self._authorization_commit = self._authorization_commit_factory(
                self.session_state
            )
        assembly = assemble_tool_capabilities(
            self.session_state,
            self.services,
            self.runtime_resources,
            workspace_dir=self.session_state.workspace_dir,
            cwd_provider=self.session_state.get_cwd,
            execution_backend=self.executor.backend,
            execution_journal_factory=self._execution_journal_factory,
            authorization_commit_factory=self._authorization_commit_factory,
            expose_autonomy=self._expose_autonomy,
        )
        self.executor.bind_run(
            assembly, authorization_commit=self._authorization_commit
        )

    def _is_cancelled(self) -> bool:
        return self.cancellation.is_cancelled()

    def _run_with_cancellation(
        self,
        max_steps: int,
        *,
        cancellation_check: Callable[[], bool] | None,
        record_memory: bool,
    ) -> str | None:
        with self.cancellation.bind_run(cancellation_check):
            return self._run_loop(max_steps, record_memory=record_memory)

    def _stop_if_cancelled(self, *, record_memory: bool = True) -> bool:
        if not self._is_cancelled():
            return False
        self.session_state.mark_cancelled()
        active_run = self.session_state.active_run()
        if active_run is not None:
            self.runtime_resources.responses.finish_response(active_run.run_id)
        if record_memory:
            self._finalize_memory(None, extract_semantic=False)
        self._checkpoint()
        self._emit_agent_stop("cancelled", reason="agent cancellation requested")
        return True

    def run(
        self,
        prompt: str,
        max_steps: int | None = None,
        *,
        cancellation_check: Callable[[], bool] | None = None,
        attachment_ids: Sequence[str] = (),
    ) -> str | None:
        """执行新任务。"""
        max_steps = self.session_state.max_steps if max_steps is None else max_steps
        if self.profile.max_steps is not None:
            max_steps = min(max_steps, self.profile.max_steps)
        if max_steps <= 0:
            raise ValueError("max_steps 必须 > 0")
        self.session_state.max_steps = max_steps
        # 计划是 user-turn 级状态，不是跨任务记忆。
        # 首轮允许装配层预置；后续 run() 开始新目标时清空，continue_run() 则保留。
        if self.session_state.turns:
            self.session_state.plan_manager.reset()
        # 重置上一轮的终态,使 status 始终反映"当前这轮"(多轮 REPL 下尤其需要)。
        attachment_ids = tuple(attachment_ids)
        records = self.session_state.attachment_records(attachment_ids)
        if len(records) > MAX_ATTACHMENTS_PER_TURN:
            raise ValueError("a turn may include at most 10 images")
        if sum(record.size for record in records) > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("turn attachments exceed 50 MiB limit")
        if not prompt.strip() and not records:
            raise ValueError("a user message needs text or an attachment")
        turn_goal = prompt or f"[{len(records)} attached image(s)]"
        self.session_state.begin_user_turn(turn_goal)
        self._bind_executor_run()
        active_run = self.session_state.active_run()
        if active_run is not None:
            self.runtime_resources.responses.begin_response(active_run.run_id)
            active_run.model_config = {
                "model": str(getattr(self.llm, "model", "")),
                "transport": str(getattr(self.llm, "transport_name", "")),
            }
            bind_journal = getattr(self._execution_journal, "bind_run", None)
            if callable(bind_journal):
                bind_journal(active_run.run_id)
            if self.on_run_started is not None:
                self.on_run_started(active_run.run_id)
        prompt_decision = None
        if self.session_state.agent_task_id is None:
            prompt_decision = self._emit_lifecycle(
                "user_prompt_submit", {
                    "prompt": prompt,
                    "attachments": [record.to_dict() for record in records],
                }
            )
            if prompt_decision is not None and prompt_decision.decision == "deny":
                self.session_state.mark_failed()
                self.ui.on_final(
                    f"A lifecycle hook rejected the request: {prompt_decision.reason}"
                )
                self._checkpoint()
                self._emit_agent_stop("failed", reason=prompt_decision.reason)
                return None
        self.session_state.append_user_message(prompt, attachment_ids)
        self._record_run_event(
            "user_input",
            {
                "session_run_id": active_run.run_id if active_run is not None else "",
                "prompt": prompt,
                "attachment_ids": list(attachment_ids),
            },
            event_key=f"user:{active_run.run_id}" if active_run is not None else "",
        )
        if prompt_decision is not None and prompt_decision.additional_context:
            self.session_state.append_message({
                "role": "user",
                "content": (
                    "<hook-additional-context>\n"
                    f"{prompt_decision.additional_context}\n"
                    "</hook-additional-context>"
                ),
            }, source="system_feedback")

        # Recall is projected onto the current user turn only. It is not appended
        # to the transcript, so later turns do not accumulate old recall blocks.
        self._recover_pending_episodes()
        self._checkpoint()
        self._emit_agent_start(turn_goal)

        return self._run_with_cancellation(
            max_steps,
            cancellation_check=cancellation_check,
            record_memory=True,
        )

    def run_runtime_event(
        self, event: dict, max_steps: int | None = None,
    ) -> str | None:
        try:
            return self._run_runtime_event(event, max_steps)
        finally:
            self._render_usage_summary()
            self._checkpoint()

    def _run_runtime_event(
        self,
        event: dict,
        max_steps: int | None = None,
    ) -> str | None:
        """Let root react to an internal event without forging a user turn.

        The current user goal, plan, evidence boundary, root-turn identity and
        episode-memory boundary stay intact. The event is still model-visible
        as a clearly typed data message and receives its own lifecycle trace.
        """
        if self.session_state.agent_task_id is not None:
            raise ValueError("只有 root Agent 可以处理 runtime event")
        budget = self.session_state.max_steps if max_steps is None else max_steps
        if budget <= 0:
            raise ValueError("max_steps 必须 > 0")
        try:
            content = json.dumps(
                {"runtime_event": event}, ensure_ascii=False, default=repr
            )
        except Exception as exc:
            raise ValueError(f"runtime event 无法序列化: {exc}") from exc
        if len(content) > 12_000:
            content = json.dumps({
                "runtime_event": {
                    "type": str(event.get("type") or "unknown")[:200],
                    "truncated": True,
                    "preview": content[:8_000],
                }
            }, ensure_ascii=False)

        active_run = self.session_state.active_run()
        if active_run is None:
            raise ValueError("runtime event requires an active Run")
        if active_run.status in {"completed", "failed", "cancelled", "interrupted"}:
            active_run = self.session_state.begin_continuation_run(
                str(event.get("type") or "runtime_event"), source="runtime_event"
            )
        self._bind_executor_run()
        self.runtime_resources.responses.begin_response(active_run.run_id)
        self._emit_lifecycle("runtime_event", event)
        self.session_state.append_message(
            {"role": "user", "content": content}, source="runtime_event"
        )
        self._checkpoint()
        self._emit_agent_start(
            str(event.get("type") or "runtime_event"), source="runtime_event"
        )
        result = self._run_loop(budget, record_memory=False, render_summary=False)
        task = event.get("task") if isinstance(event.get("task"), dict) else {}
        event_root_turn_id = str(task.get("root_turn_id") or "")
        current_turn_id = self.session_state.agent_root_turn_id
        settle_previous = (
            getattr(self.memory, "settle_previous_turn", None)
            if self.memory is not None else None
        )
        if (
            callable(settle_previous)
            and event_root_turn_id
            and event_root_turn_id != current_turn_id
        ):
            settle_previous(
                self.session_state,
                root_turn_id=event_root_turn_id,
                task=task,
            )
            self._checkpoint()
        elif (
            self.memory is not None
            and result is not None
            and (not event_root_turn_id or event_root_turn_id == current_turn_id)
            and current_turn_id not in self._memory_finalized_turns
        ):
            self._finalize_memory(result, extract_semantic=True)
            self._checkpoint()
        return result

    def continue_run(
        self,
        max_steps: int | None = None,
        *,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> str | None:
        """Continue a running session loaded from a checkpoint.

        This is intentionally separate from ask_user: human interaction remains
        synchronous inside the permission layer and has no pause/resume state.
        """
        if self.session_state.current_run_status() != "running":
            raise ValueError(
                "只能继续 running 的 Run，当前为 "
                f"{self.session_state.current_run_status()}"
            )
        # A checkpoint may have been copied while an older writer still showed
        # status=running.  The commit ledger is authoritative: never call the
        # model again for a turn whose final result was already committed.
        if self.session_state.is_turn_committed():
            self.session_state.mark_completed()
            self._checkpoint()
            for turn in reversed(self.session_state.turns):
                answer = turn.parsed.get("final_answer")
                if answer is not None:
                    return str(answer)
            return None
        budget = self.session_state.max_steps if max_steps is None else max_steps
        if budget <= 0:
            raise ValueError("max_steps 必须 > 0")
        if (
            self.session_state.agent_task_id is None
            and not self.session_state.agent_root_turn_id
        ):
            # 兼容第三阶段之前生成、尚未带控制面 turn id 的 running checkpoint。
            self.session_state.agent_root_turn_id = (
                f"{self.session_state.session_id}:"
                f"{self.session_state.active_turn_start_message_index}"
            )
        self._bind_executor_run()
        self._recover_pending_episodes()
        self._checkpoint()
        self._emit_agent_start(self.session_state.current_goal(), resumed=True)
        return self._run_with_cancellation(
            budget,
            cancellation_check=cancellation_check,
            record_memory=True,
        )

    def attach_root_checkpoint(self) -> None:
        """Subscribe the control plane to this agent's checkpoint writer."""
        if self.checkpoint_store is None or self.session_state.agent_task_id is not None:
            return
        self.session_state.control_plane.set_on_change(self._checkpoint)

    def _checkpoint(self) -> None:
        if self.checkpoint_store is None:
            return
        try:
            self.checkpoint_store.save(self.session_state)
            self.last_checkpoint_error = None
        except Exception as exc:
            # Persistence is a reliability sidecar: surface the failure for
            # callers/tests, but do not destroy an otherwise valid agent turn.
            self.last_checkpoint_error = exc
            self.ui.on_checkpoint_error(f"{type(exc).__name__}: {exc}")

    def checkpoint(self) -> None:
        """Persist process-local orchestration fields changed outside Agent."""
        self._checkpoint()

    def _recover_pending_episodes(self) -> None:
        recover = getattr(self.memory, "recover_pending", None)
        if self.memory is None or not callable(recover):
            return
        before = list(self.session_state.pending_episode_finalizes)
        recover(self.session_state)
        if self.session_state.pending_episode_finalizes != before:
            self._checkpoint()

    def _finalize_memory(
        self,
        final_answer: str | None,
        *,
        extract_semantic: bool,
        termination_reason: str | None = None,
    ) -> None:
        if self.memory is not None:
            outcome = self.memory.finalize_turn(
                self.session_state,
                final_answer,
                extract_semantic=extract_semantic,
                termination_reason=termination_reason,
            )
            if outcome.get("episode_id") is not None:
                self._memory_finalized_turns.add(
                    self.session_state.agent_root_turn_id
                )

    def _has_live_agent_tasks(self, root_turn_id: str) -> bool:
        def live(nodes: list[dict]) -> bool:
            for node in nodes:
                if node.get("status") in {"pending", "running"}:
                    return True
                children = node.get("children")
                if isinstance(children, list) and live(children):
                    return True
            return False

        return live(self.session_state.control_plane.tree(root_turn_id))

    def _terminate(
        self,
        status: str,
        *,
        reason: str,
        message: str,
        record_memory: bool,
    ) -> None:
        """Single exit path: render, mark, persist memory, checkpoint, emit."""
        self.ui.on_final(message)
        getattr(self.session_state, self._TERMINAL_MARKERS[status])()
        active_run = self.session_state.active_run()
        if active_run is not None:
            self.runtime_resources.responses.finish_response(active_run.run_id)
        if record_memory:
            self._finalize_memory(
                None,
                extract_semantic=False,
                termination_reason=status,
            )
        self._checkpoint()
        self._emit_agent_stop(status, reason=reason)

    def _turn_control(self, *, record_memory: bool) -> TurnControl:
        def finalize_memory(answer: str | None, *, extract_semantic: bool) -> None:
            if record_memory:
                self._finalize_memory(answer, extract_semantic=extract_semantic)

        def terminate(status: str, *, reason: str, message: str) -> None:
            self._terminate(
                status,
                reason=reason,
                message=message,
                record_memory=record_memory,
            )

        return TurnControl(
            session=self.session_state,
            verifier=self.verifier,
            ui=self.ui,
            executor=self.executor,
            responses=self.runtime_resources.responses,
            max_verification_retries=self.max_verification_retries,
            max_consecutive_invalid=self.max_consecutive_invalid,
            record_run_event=self._record_run_event,
            record_usage=self._record_usage_for_turn,
            stop_if_cancelled=lambda: self._stop_if_cancelled(record_memory=record_memory),
            checkpoint=self._checkpoint,
            emit_agent_stop=self._emit_agent_stop,
            finalize_memory=finalize_memory,
            terminate=terminate,
        )

    def _handle_final_turn(
        self,
        turn,
        content: str,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        counters: RetryCounters,
        *,
        record_memory: bool,
    ) -> tuple[str | None, str]:
        return handle_final_turn(
            self._turn_control(record_memory=record_memory),
            turn,
            content,
            usage_record,
            transient_plan_tokens,
            counters,
        )

    def _handle_tool_calls_turn(
        self,
        turn,
        content: str,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        *,
        record_memory: bool,
    ) -> bool:
        return handle_tool_calls_turn(
            self._turn_control(record_memory=record_memory),
            turn,
            content,
            usage_record,
            transient_plan_tokens,
        )

    def _handle_invalid_turn(
        self,
        response: ContentDone,
        error: TurnAbort,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        counters: RetryCounters,
        *,
        record_memory: bool,
    ) -> str:
        return handle_invalid_turn(
            self._turn_control(record_memory=record_memory),
            response,
            error,
            usage_record,
            transient_plan_tokens,
            counters,
        )

    def _run_loop(
        self, max_steps: int, *, record_memory: bool = True,
        render_summary: bool = True,
    ) -> str | None:
        try:
            return self._run_loop_impl(max_steps, record_memory=record_memory)
        finally:
            if render_summary:
                self._render_usage_summary()
            self._checkpoint()

    def _run_loop_impl(
        self,
        max_steps: int,
        *,
        record_memory: bool = True,
    ) -> str | None:
        """运行当前 user turn 直到 final_answer 或耗尽步数。"""
        counters = RetryCounters()
        for _ in range(max_steps):
            if self._stop_if_cancelled(record_memory=record_memory):
                return None
            reminders = self._ephemeral_reminders()
            transient = sum(
                estimate_message_tokens(message) for message in reminders
            )
            try:
                response, usage_record = self._run_turn(reminders)
            except ContextBudgetExceeded as exc:
                self._terminate(
                    "failed",
                    reason="context budget exceeded",
                    message=f"The context could not be built inside the budget: {exc}",
                    record_memory=record_memory,
                )
                return None
            content = response.content
            try:
                turn = parse_turn(response)
                if any(
                    call.id in self.session_state.tool_executions
                    for call in turn.tool_calls
                ):
                    raise TurnAbort("Provider reused a tool call ID from an earlier turn")
                for call, recorded in zip(
                    turn.tool_calls, turn.parsed["tool_calls"], strict=True
                ):
                    wire_name = call.name
                    if wire_name not in self._tool_names:
                        raise TurnAbort(
                            f"Tool is not available this turn: {wire_name}"
                        )
                    call.name = self._tool_names[wire_name]
                    self.session_state.touch_active_deferred_tool(call.name)
                    recorded["name"] = call.name
                counters.invalid = 0
                if turn.kind == "final":
                    answer, outcome = self._handle_final_turn(
                        turn, content, usage_record, transient, counters,
                        record_memory=record_memory,
                    )
                    if outcome == "done":
                        return answer
                    if outcome != "retry":
                        return None
                elif turn.kind == "tool_calls":
                    if not self._handle_tool_calls_turn(
                        turn, content, usage_record, transient,
                        record_memory=record_memory,
                    ):
                        return None
            except TurnAbort as error:
                outcome = self._handle_invalid_turn(
                    response, error, usage_record, transient, counters,
                    record_memory=record_memory,
                )
                if outcome != "retry":
                    return None
        self._terminate(
            "max_steps",
            reason="max steps reached",
            message=f"The step limit ({max_steps}) was reached before the task finished.",
            record_memory=record_memory,
        )
        return None
