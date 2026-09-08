import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from openai.types.chat import ChatCompletionMessageParam

from .context import ContextCompactor
from .events import ContentDelta, ContentDone, ReasoningDelta, UsageEvent
from .executor import ToolExecutor
from .llm import LLMClient
from .logger import get_logger
from .memory import MemoryManager
from .permission import PermissionResolver
from .prompt import build_system_prompt
from .protocol import TurnAbort, encode_tools, parse_turn
from .renderer import Renderer
from .services import RuntimeServices
from .session import SessionState, UsageRecord
from .skills.prompt import catalog_reminder
from .skills.registry import SkillRegistry
from .tools.base import Tool, ToolResult
from .util import build_tool_results_messages, estimate_message_tokens
from .verifier import Verifier

if TYPE_CHECKING:
    from .checkpoint import SessionCheckpointStore

logger = get_logger(__name__)


@dataclass
class _RetryCounters:
    invalid: int = 0
    verifier: int = 0
    hook: int = 0


class Agent:
    _TERMINAL_MARKERS: ClassVar[dict[str, str]] = {
        "failed": "mark_failed",
        "max_steps": "mark_max_steps",
    }
    def __init__(
        self,
        llm: LLMClient,
        tools: list[Tool],
        session_state: SessionState,
        renderer: Renderer,
        tool_timeout: float = 30,
        context_watermark: float = 0.75,
        keep_recent_tool_results: int = 3,
        max_consecutive_invalid: int = 3,
        permission_resolver: PermissionResolver | None = None,
        cancellation_check: Callable[[], bool] | None = None,
        memory: MemoryManager | None = None,
        verifier: Verifier | None = None,
        max_verification_retries: int = 3,
        checkpoint_store: "SessionCheckpointStore | None" = None,
        usage_observer: Callable[[UsageRecord], None] | None = None,
        allow_background_tasks: bool = True,
        on_shell_task_done: Callable[[str], None] | None = None,
        lifecycle=None,
        skills: SkillRegistry | None = None,
        services: RuntimeServices | None = None,
    ):
        self.llm = llm
        self.session_state = session_state
        self.renderer = renderer
        self.services = services
        # 长期记忆协作者:只主 Agent 注入,子 Agent 传 None(保持纯净隔离上下文)。
        # Agent 只在主循环里喊它三声:构造时取指令、每轮注入召回、收口后提取落盘。
        self.memory = memory
        # Skill 目录只写入 transcript 一次；正文走 skill 工具的 tool_result。
        self.skills = skills
        self.verifier = verifier
        self.max_verification_retries = max_verification_retries
        self.checkpoint_store = checkpoint_store
        self.last_checkpoint_error: Exception | None = None
        self._usage_observer = usage_observer
        if self.memory is not None:
            self.memory.usage_observer = self._record_auxiliary_usage
        self.lifecycle = lifecycle
        self._memory_finalized_turns: set[str] = set()
        if max_verification_retries < 1:
            raise ValueError("max_verification_retries 必须 >= 1")
        # 权限裁决器可由装配层注入(承载规则/模式配置),并沿主→子 Agent 共用同一份;
        # 不传则 ToolExecutor 自建一个无 handler 的默认 resolver(ask 一律 fail-closed)。
        self._permission_resolver = permission_resolver
        self._cancellation_check = cancellation_check
        self._active_run_cancellation_check: Callable[[], bool] | None = None
        # 连续 N 轮响应不完整或工具调用无效就止损,
        # 与其烧光 max_steps,不如如实标 failed 退出。中间成功一次即清零。
        self.max_consecutive_invalid = max_consecutive_invalid

        # 上下文压缩独立成 collaborator:Agent 只负责在主循环里喊它一声 +
        # 折叠后从 running total 扣减省下的 token,折叠逻辑本身归 ContextCompactor。
        self.compactor = ContextCompactor(
            renderer,
            context_watermark=context_watermark,
            keep_recent_tool_results=keep_recent_tool_results,
        )

        # Schemas are request-local: parent and child agents may share one client.
        self.tool_schemas, self._tool_names = encode_tools(tools)
        if not self.session_state.message_records:
            memory_section = self.memory.instructions() if self.memory else ""
            self.session_state.append_message({
                "role": "system",
                "content": build_system_prompt(memory_section=memory_section),
            })

        # 工具调度执行独立成 collaborator:Agent 只在主循环里把这一轮的 tool_calls
        # 交给它,查表/钳超时/并发分流/异常兜底都归 ToolExecutor。
        # registry 存整个 Tool:执行要 call,调度要 concurrency 等元数据。
        self.executor = ToolExecutor(
            {tool.name: tool for tool in tools},
            tool_timeout=tool_timeout,
            on_command_output=renderer.on_command_output,
            on_progress=renderer.on_agent_event,
            on_shell_task_done=on_shell_task_done,
            permission_resolver=permission_resolver,
            session_state=session_state,
            # Use the composed check so an autonomous durable run can add its
            # own cancellation signal without rebuilding the executor.
            cancellation_check=self._is_cancelled,
            allow_background_tasks=allow_background_tasks,
            lifecycle=lifecycle,
            services=services,
        )
        if (
            checkpoint_store is not None
            and self.session_state.agent_task_id is None
        ):
            # 子任务在工具调用内部运行；控制面状态改变时也要主动落 root
            # checkpoint，否则进程崩溃会只留下一个看不见的 pending spawn。
            self.session_state.control_plane.set_on_change(self._checkpoint)

    @property
    def context_limit(self) -> int | None:
        return self.llm.context_limit

    @property
    def messages(self) -> Sequence[ChatCompletionMessageParam]:
        return self.session_state.messages

    def _compact_context_if_needed(self, transient_tokens: int = 0) -> int:
        """喊 compactor 折叠旧工具结果;折叠后从 running total 扣减省下的 token。

        不再作废锚点:running total 被增量调整(减去折叠省下的),
        下次 usage 回来时更新估算锚点。
        """
        context_tokens = self.session_state.context_tokens + transient_tokens
        should_compact = bool(
            self.context_limit is not None
            and context_tokens > self.context_limit * self.compactor.context_watermark
        )
        if should_compact:
            self._emit_lifecycle("pre_compact", {
                "context_tokens": context_tokens,
                "context_limit": self.context_limit,
                "watermark": self.compactor.context_watermark,
            })
        folded_count, token_savings = self.compactor.compact_if_needed(
            self.session_state.message_records,
            context_tokens,
            self.context_limit,
        )
        if token_savings:
            self.session_state.context_tokens -= token_savings
        if should_compact:
            self._emit_lifecycle("post_compact", {
                "folded_count": folded_count,
                "token_savings": token_savings,
                "context_tokens": self.session_state.context_tokens,
            })
        return folded_count

    def _emit_lifecycle(self, event: str, payload: dict):
        if self.lifecycle is None:
            return None
        return self.lifecycle.emit(
            event,
            payload,
            agent_task_id=self.session_state.agent_task_id,
            root_turn_id=self.session_state.agent_root_turn_id,
        )

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
        reminders: Sequence[ChatCompletionMessageParam] = (),
    ) -> tuple[ContentDone, UsageRecord | None]:
        """渲染事件流，返回包含正文和工具调用的完整响应。

        ``reminders`` 只叠在本轮请求末尾，不写入 transcript。调用方先算
        token 再传入同一份列表，用量校准才能扣掉这笔临时开销。
        """

        response = ContentDone("", finish_reason="incomplete")
        usage_record: UsageRecord | None = None
        self.renderer.on_turn_begin()

        wire_messages = self.session_state.wire_messages()
        wire_messages.extend(reminders)

        self._emit_lifecycle("llm_start", {
            "model": str(getattr(self.llm, "model", "")),
            "message_count": len(wire_messages),
            "context_tokens": self.session_state.context_tokens,
            "has_plan_reminder": any(
                "<plan-state>" in str(message.get("content", ""))
                for message in reminders
            ),
        })
        started = time.monotonic()
        try:
            for event in self.llm(wire_messages, tools=self.tool_schemas):
                if isinstance(event, ReasoningDelta):
                    self.renderer.on_reasoning_delta(event.piece)
                elif isinstance(event, ContentDelta):
                    self.renderer.on_content_delta(event.piece)
                elif isinstance(event, ContentDone):
                    response = event
                elif isinstance(event, UsageEvent):
                    usage_record = UsageRecord.from_usage(event.usage)

        except Exception as exc:
            if usage_record is not None:
                self._record_auxiliary_usage(usage_record)
            self._emit_lifecycle("llm_error", {
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.monotonic() - started) * 1_000, 3),
            })
            raise

        self.renderer.on_usage(
            usage_record.prompt_tokens if usage_record else None,
            usage_record.completion_tokens if usage_record else None,
            usage_record.total_tokens if usage_record else None,
            self.context_limit,
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

    def _plan_reminder(self) -> ChatCompletionMessageParam | None:
        block = self.session_state.plan_manager.to_prompt_block()
        # 计划字段由模型工具调用产生，最终也可能来自不可信用户文本；保持 user role，
        # 并由 to_prompt_block 的 JSON 数据边界明确它不具备指令权限。
        return {"role": "user", "content": block} if block else None

    def _ensure_skill_catalog(self) -> None:
        """会话里只把 skill 目录写入 transcript 一次。"""
        if self.skills is None or self.session_state.skill_catalog_sent:
            return
        catalog = catalog_reminder(self.skills.list_metas())
        if catalog:
            self.session_state.append_message({"role": "user", "content": catalog})
        self.session_state.mark_skill_catalog_sent()

    def _ephemeral_reminders(self) -> list[ChatCompletionMessageParam]:
        """本轮才需要、不能落进会话记录的提醒。目前只有最新计划块。"""
        reminders: list[ChatCompletionMessageParam] = []
        plan = self._plan_reminder()
        if plan is not None:
            reminders.append(plan)
        return reminders

    def _record_auxiliary_usage(self, usage: UsageRecord) -> None:
        self.session_state.add_usage(usage)
        self._notify_usage(usage)

    def _render_usage_summary(self) -> None:
        if self._has_live_agent_tasks(self.session_state.agent_root_turn_id):
            return
        usage = self.session_state.task_usage()
        self.renderer.on_usage_summary(
            usage.prompt_tokens, usage.completion_tokens, usage.total_tokens,
        )

    def _record_usage_for_turn(
        self,
        turn_record,
        usage_record: UsageRecord,
        transient_plan_tokens: int,
    ) -> None:
        self.session_state.record_usage_for_turn(turn_record, usage_record)
        # 服务端 prompt_tokens 包含临时计划提醒，但它没有落进 transcript；扣除其估算值，
        # 令 running total 始终表示持久 transcript。下一轮压缩时会重新加上最新提醒。
        self.session_state.context_tokens = max(
            0, self.session_state.context_tokens - transient_plan_tokens
        )
        self._notify_usage(usage_record)

    def _notify_usage(self, usage_record: UsageRecord) -> None:
        if self._usage_observer is not None:
            try:
                self._usage_observer(usage_record)
            except Exception:
                # 计量旁路失败不能破坏当前消息账本；控制面仍可用 step 上限止损。
                logger.debug("usage observer failed", exc_info=True)

    def _is_cancelled(self) -> bool:
        return bool(
            (self._cancellation_check and self._cancellation_check())
            or (
                self._active_run_cancellation_check
                and self._active_run_cancellation_check()
            )
        )

    def _run_with_cancellation(
        self,
        max_steps: int,
        *,
        cancellation_check: Callable[[], bool] | None,
        record_memory: bool,
    ) -> str | None:
        previous = self._active_run_cancellation_check
        self._active_run_cancellation_check = cancellation_check
        try:
            return self._run_loop(max_steps, record_memory=record_memory)
        finally:
            self._active_run_cancellation_check = previous

    def _stop_if_cancelled(self, *, record_memory: bool = True) -> bool:
        if not self._is_cancelled():
            return False
        self.session_state.mark_failed()
        if record_memory:
            self._finalize_memory(None, extract_semantic=False)
        self._checkpoint()
        self._emit_agent_stop("cancelled", reason="agent cancellation requested")
        return True

    def run(self, prompt: str, max_steps: int | None = None) -> str | None:
        """执行新任务。"""
        max_steps = self.session_state.max_steps if max_steps is None else max_steps
        if max_steps <= 0:
            raise ValueError("max_steps 必须 > 0")
        self.session_state.max_steps = max_steps
        # 计划是 user-turn 级状态，不是跨任务记忆。
        # 首轮允许装配层预置；后续 run() 开始新目标时清空，continue_run() 则保留。
        if self.session_state.turns:
            self.session_state.plan_manager.reset()
        # 重置上一轮的终态,使 status 始终反映"当前这轮"(多轮 REPL 下尤其需要)。
        self.session_state.mark_running()
        self.session_state.begin_user_turn(prompt)
        prompt_decision = None
        if self.session_state.agent_task_id is None:
            prompt_decision = self._emit_lifecycle(
                "user_prompt_submit", {"prompt": prompt}
            )
            if prompt_decision is not None and prompt_decision.decision == "deny":
                self.session_state.mark_failed()
                self.renderer.on_final(
                    f"用户请求被 lifecycle hook 拒绝：{prompt_decision.reason}"
                )
                self._checkpoint()
                self._emit_agent_stop("failed", reason=prompt_decision.reason)
                return None
        self.session_state.append_message({"role": "user", "content": prompt})
        if prompt_decision is not None and prompt_decision.additional_context:
            self.session_state.append_message({
                "role": "user",
                "content": (
                    "<hook-additional-context>\n"
                    f"{prompt_decision.additional_context}\n"
                    "</hook-additional-context>"
                ),
            })

        # 自动召回:针对本轮 prompt 选出相关记忆 + MEMORY.md 索引,作为 system-reminder
        # 注入(role=user 以兼容各端点)。走 append_message 自动计入 context_tokens。
        # 召回是尽力而为的旁路,内部已吞异常,空块则跳过。
        if self.memory:
            recall_block = self.memory.recall_block(prompt)
            if recall_block:
                self.session_state.append_message(
                    {"role": "user", "content": recall_block}
                )

        self._ensure_skill_catalog()
        self._checkpoint()
        self._emit_agent_start(prompt)

        return self._run_loop(max_steps)

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
        episodic-memory boundary stay intact. The event is still model-visible
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

        self.session_state.mark_running()
        self._emit_lifecycle("runtime_event", event)
        self.session_state.append_message({"role": "user", "content": content})
        self._checkpoint()
        self._emit_agent_start(
            str(event.get("type") or "runtime_event"), source="runtime_event"
        )
        result = self._run_loop(budget, record_memory=False, render_summary=False)
        event_root_turn_id = str(
            (event.get("task") or {}).get("root_turn_id")
            if isinstance(event.get("task"), dict) else ""
        )
        current_turn_id = self.session_state.agent_root_turn_id
        if (
            self.memory is not None
            and result is not None
            and event_root_turn_id == current_turn_id
            and current_turn_id not in self._memory_finalized_turns
            and not self._has_live_agent_tasks(current_turn_id)
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
        if self.session_state.status != "running":
            raise ValueError(
                f"只能继续 status=running 的会话，当前为 {self.session_state.status}"
            )
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
        self._ensure_skill_catalog()
        self._checkpoint()
        self._emit_agent_start(self.session_state.user_goal, resumed=True)
        return self._run_with_cancellation(
            budget,
            cancellation_check=cancellation_check,
            record_memory=True,
        )

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
            self.renderer.on_checkpoint_error(f"{type(exc).__name__}: {exc}")

    def checkpoint(self) -> None:
        """Persist process-local orchestration fields changed outside Agent."""
        self._checkpoint()

    def _finalize_memory(
        self, final_answer: str | None, *, extract_semantic: bool
    ) -> None:
        if self.memory is not None:
            outcome = self.memory.finalize_turn(
                self.session_state,
                final_answer,
                extract_semantic=extract_semantic,
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
        self.renderer.on_final(message)
        getattr(self.session_state, self._TERMINAL_MARKERS[status])()
        if record_memory:
            self._finalize_memory(None, extract_semantic=False)
        self._checkpoint()
        self._emit_agent_stop(status, reason=reason)

    def _handle_final_turn(
        self,
        turn,
        content: str,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        counters: _RetryCounters,
        *,
        record_memory: bool,
    ) -> tuple[str | None, str]:
        """Return (final_answer, outcome) where outcome is
        'done' | 'retry' | 'terminated' | 'cancelled'."""
        turn_record = self.session_state.record_assistant_turn(
            assistant_raw=content,
            parsed=turn.parsed,
            assistant_message=turn.assistant_message,
            route="final",
        )
        if usage_record is not None:
            self._record_usage_for_turn(
                turn_record, usage_record, transient_plan_tokens
            )
        if self._stop_if_cancelled(record_memory=record_memory):
            return None, "cancelled"

        verification = (
            self.verifier.verify(self.session_state, turn.final_answer)
            if self.verifier
            else None
        )
        if verification is not None:
            self.session_state.record_verification(
                turn_record,
                verification.approved,
                [issue.to_dict() for issue in verification.issues],
            )
            if not verification.approved:
                counters.verifier += 1
                self.renderer.on_completion_rejected(verification.issues)
                self.session_state.append_message(
                    verification.feedback_message()
                )
                self._checkpoint()
                if counters.verifier >= self.max_verification_retries:
                    self._terminate(
                        "failed",
                        reason="completion verification retry limit",
                        message="最终答案连续未通过完成验证，任务终止。",
                        record_memory=record_memory,
                    )
                    return None, "terminated"
                return None, "retry"
            counters.verifier = 0

        stop_decision = self._emit_agent_stop(
            "completed", final_answer=turn.final_answer
        )
        if stop_decision is not None and stop_decision.decision == "deny":
            counters.hook += 1
            self.session_state.append_message({
                "role": "user",
                "content": json.dumps(
                    {
                        "error": "agent_stop hook rejected completion",
                        "reason": stop_decision.reason,
                    },
                    ensure_ascii=False,
                ),
            })
            self._checkpoint()
            if counters.hook >= self.max_verification_retries:
                self._terminate(
                    "failed",
                    reason="agent_stop hook retry limit",
                    message="最终答案连续未通过 lifecycle hook，任务终止。",
                    record_memory=record_memory,
                )
                return None, "terminated"
            return None, "retry"
        counters.hook = 0

        self.renderer.on_final(turn.final_answer)
        self.session_state.mark_completed()
        # 每个终态都记录 episode；只有成功回合才提取长期语义记忆。
        # 若同 turn 仍有后台 Agent，等最后一条 runtime notification
        # 收口后再一次性写 episode，避免把 running 摘要永久固化。
        if (
            record_memory
            and not self._has_live_agent_tasks(
                self.session_state.agent_root_turn_id
            )
        ):
            self._finalize_memory(
                turn.final_answer, extract_semantic=True
            )
        self._checkpoint()
        return turn.final_answer, "done"

    def _handle_tool_calls_turn(
        self,
        turn,
        content: str,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        *,
        record_memory: bool,
    ) -> bool:
        """Return False if the run was cancelled mid-turn."""
        turn_record = self.session_state.record_assistant_turn(
            assistant_raw=content,
            parsed=turn.parsed,
            assistant_message=turn.assistant_message,
            route="tool_calls",
            tool_calls=turn.tool_calls,
        )
        if usage_record is not None:
            self._record_usage_for_turn(
                turn_record, usage_record, transient_plan_tokens
            )
        if self._stop_if_cancelled(record_memory=record_memory):
            cancelled = []
            for call in turn.tool_calls:
                result = ToolResult.fail("Cancelled before tool execution")
                self.session_state.record_tool_execution(call.id, result)
                cancelled.append((call, result))
            for message in build_tool_results_messages(cancelled):
                self.session_state.append_message(message)
            self._checkpoint()
            return False

        # 工具副作用前先落 pending checkpoint。若进程在调用期间崩溃，
        # 恢复层会把这些调用标成 outcome unknown 并要求模型先检查现场，
        # 不会把同一个写操作静默重放。
        self._checkpoint()

        outcomes = self.executor.execute(
            turn.tool_calls,
            on_call=self.renderer.on_tool_call,
            on_result=self.renderer.on_tool_result,
        )

        for outcome in outcomes:
            self.session_state.record_tool_execution(
                call_id=outcome.call.id,
                result=outcome.result,
                status=outcome.status,
            )

        for message in build_tool_results_messages(
            [(outcome.call, outcome.result) for outcome in outcomes]
        ):
            self.session_state.append_message(message)
        self._checkpoint()
        return True

    def _handle_invalid_turn(
        self,
        response: ContentDone,
        error: TurnAbort,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        counters: _RetryCounters,
        *,
        record_memory: bool,
    ) -> str:
        """Return 'retry' | 'terminated' | 'cancelled'."""
        counters.invalid += 1
        turn_record = self.session_state.record_invalid_turn(
            response.content,
            f"LLM output could not be parsed or routed: {error}",
            parsed={
                "response": response.assistant_message(),
                "finish_reason": response.finish_reason,
            },
        )
        if usage_record is not None:
            self._record_usage_for_turn(
                turn_record, usage_record, transient_plan_tokens
            )
        if self._stop_if_cancelled(record_memory=record_memory):
            return "cancelled"

        if counters.invalid >= self.max_consecutive_invalid:
            self._terminate(
                "failed",
                reason="invalid output retry limit",
                message=(
                    f"连续 {counters.invalid} 轮输出无法解析，任务终止。"
                ),
                record_memory=record_memory,
            )
            return "terminated"

        self.session_state.append_message({
            "role": "user",
            "content": json.dumps(
                {"error": f"LLM output could not be parsed or routed: {error}"},
                ensure_ascii=False,
            ),
        })
        self._checkpoint()
        return "retry"

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
        counters = _RetryCounters()
        for _ in range(max_steps):
            if self._stop_if_cancelled(record_memory=record_memory):
                return None
            reminders = self._ephemeral_reminders()
            transient = sum(
                estimate_message_tokens(message) for message in reminders
            )
            self._compact_context_if_needed(transient)
            response, usage_record = self._run_turn(reminders)
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
                    call.name = self._tool_names.get(call.name, call.name)
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
            message=f"已达到最大步数上限（{max_steps} 步），任务未完成。",
            record_memory=record_memory,
        )
        return None
