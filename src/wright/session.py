from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeAlias
from uuid import uuid4

from .conversation import ConversationMessage, ImagePart, TextPart, UserTurnInput
from .coordination import AgentControlPlane
from .planning import PlanManager
from .project import ExecutionEnvironment
from .runs import TERMINAL_RUN_STATUSES, RunRecord, RunStatus, new_run_id
from .tools.base import ToolCall, ToolResult
from .util import estimate_message_tokens

if TYPE_CHECKING:
    from .attachments import AttachmentRecord

CallId: TypeAlias = str
MessageId: TypeAlias = str
SessionLifecycle = Literal["open", "closing", "closed"]
TurnRoute = Literal["tool_calls", "final", "invalid"]
ToolExecutionTerminal = Literal["succeeded", "failed", "timeout"]
ToolExecutionStatus = Literal["pending", "running"] | ToolExecutionTerminal


@dataclass
class Session:
    session_id: str
    workspace_dir: Path
    cwd: Path

    turns: list[TurnRecord]
    # wire 内容唯一存放在 MessageRecord.message;turns 只用稳定 id 贴注解,
    # 不依赖 message_records 的当前位置。
    message_records: list[MessageRecord]

    tool_executions: dict[CallId, ToolExecutionRecord]
    background_tasks: dict[str, BackgroundTask]
    plan_manager: PlanManager
    # A stable label for the long-lived conversation, not the current task.
    # Every executable goal belongs to its RunRecord.
    session_label: str = ""
    lifecycle: SessionLifecycle = "open"
    # Durable state is grouped by project_root. workspace_dir remains the
    # actual execution directory for compatibility with existing tools.
    project_root: Path | None = None
    environment: ExecutionEnvironment = "local"
    base_commit: str | None = None
    branch_name: str | None = None
    # Authoritative per-execution state.  Legacy fields below are projections
    # retained while Agent/Tool APIs are migrated.
    runs: dict[str, RunRecord] = field(default_factory=dict)
    active_run_id: str | None = None
    # 目录只往 transcript 写一次。正文走 load_skill 的 tool_result，不另建激活表。
    skill_catalog_sent: bool = False
    # Deferred tool schemas discovered by tool_search. Order is least-recently
    # touched first so the catalog can remain bounded and survive resume.
    active_deferred_tools: list[str] = field(default_factory=list)
    # Root 与所有子 Agent 共享同一个控制面；子 session 用 agent_task_id
    # 标记自己在任务树中的位置，root 则为 None。
    control_plane: AgentControlPlane = field(default_factory=AgentControlPlane)
    agent_task_id: str | None = None
    agent_root_turn_id: str = ""
    # Durable commit markers make a completed user turn idempotent across
    # crashes.  Status is presentation state; this ledger is the replay guard.
    committed_turn_ids: list[str] = field(default_factory=list)
    # 当前 user turn 从哪个全局 step 之后开始。Verifier 用它隔离多轮 REPL 中
    # 旧任务的工具证据；checkpoint/resume 也靠它恢复本轮边界。
    active_turn_start_step: int = 0
    active_turn_start_message_index: int = 0
    last_usage: UsageRecord | None = None
    total_usage: UsageRecord = field(default_factory=lambda: UsageRecord())

    task_usage_start: UsageRecord = field(default_factory=lambda: UsageRecord())

    # The active main-model choice is session state so /model survives resume.
    model_name: str | None = None
    # Resolved once for a session.  A transcript with Responses reasoning items
    # cannot safely change wire protocols midway through a resumed task.
    llm_transport: str | None = None
    # Binary bytes belong to AttachmentStore; checkpoints keep this compact
    # registry and user messages refer to attachment ids through ``parts``.
    attachments: dict[str, AttachmentRecord] = field(default_factory=dict)

    # Durable-history estimate, updated as records are appended. It is never
    # presented as provider usage.
    context_tokens: int = 0
    # Last complete request estimate: history + transient instructions + tool
    # schemas + output reserve. Unlike ``last_usage``, this is local only.
    request_context_tokens: int = 0

    step_count: int = 0
    max_steps: int = 25
    message_id_counter: int = 0
    _cwd_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    _background_tasks_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )

    @classmethod
    def create(
        cls,
        initial_goal: str,
        workspace_dir: Path,
        max_steps: int = 50,
        *,
        session_id: str | None = None,
        project_root: Path | None = None,
        environment: ExecutionEnvironment = "local",
        base_commit: str | None = None,
        branch_name: str | None = None,
    ) -> Session:
        execution_root = workspace_dir.resolve()
        return cls(
            session_id=session_id or uuid4().hex[:6],
            session_label=initial_goal,
            workspace_dir=execution_root,
            cwd=execution_root,
            project_root=(project_root or execution_root).resolve(),
            environment=environment,
            base_commit=base_commit,
            branch_name=branch_name,
            turns=[],
            message_records=[],
            tool_executions={},
            background_tasks={},
            plan_manager=PlanManager(),
            max_steps=max_steps,
        )

    def get_cwd(self) -> Path:
        with self._cwd_lock:
            return self.cwd

    def set_cwd(self, cwd: Path) -> None:
        with self._cwd_lock:
            self.cwd = cwd.resolve()

    def register_background_task(self, task: BackgroundTask) -> None:
        """Persist task metadata; RuntimeResources owns its live handles."""
        with self._background_tasks_lock:
            self.background_tasks[task.task_id] = task

    def get_background_task(self, task_id: str) -> BackgroundTask | None:
        with self._background_tasks_lock:
            return self.background_tasks.get(task_id)

    def list_background_tasks(self) -> list[BackgroundTask]:
        """Return a stable registry snapshot; individual tasks remain live."""
        with self._background_tasks_lock:
            return list(self.background_tasks.values())

    def mark_background_tasks_cancel_requested(
        self, task_ids: tuple[str, ...], reason: str
    ) -> None:
        """Reflect a RuntimeResources shutdown in durable task metadata."""
        with self._background_tasks_lock:
            for task_id in task_ids:
                task = self.background_tasks.get(task_id)
                if task is not None:
                    task.cancel_requested = True
                    task.cancel_reason = reason[:1_000]

    def _next_step(self) -> int:
        self.step_count += 1
        return self.step_count

    def mark_skill_catalog_sent(self) -> None:
        self.skill_catalog_sent = True

    def clear_active_deferred_tools(self) -> int:
        count = len(self.active_deferred_tools)
        self.active_deferred_tools.clear()
        return count

    def touch_active_deferred_tool(self, name: str) -> None:
        if name not in self.active_deferred_tools:
            return
        self.active_deferred_tools.remove(name)
        self.active_deferred_tools.append(name)

    def begin_user_turn(self, prompt: str) -> None:
        """记录当前任务目标及其证据边界。"""
        self.task_usage_start = UsageRecord(**vars(self.total_usage))
        self.active_turn_start_step = self.step_count
        self.active_turn_start_message_index = len(self.message_records)
        self.begin_run(
            prompt,
            source="subagent" if self.agent_task_id is not None else "user_input",
        )
        if self.agent_task_id is None:
            # Legacy task-control IDs remain stable for existing subagent
            # checkpoints; RunRecord owns the new independent execution id.
            self.agent_root_turn_id = f"{self.session_id}:{self.active_turn_start_message_index}"

    def begin_run(self, goal: str, *, source: str, parent_run_id: str | None = None) -> RunRecord:
        """Create the sole writable record for one root execution."""
        run = RunRecord(new_run_id(), self.session_id, source, goal, parent_run_id=parent_run_id)
        run.root_run_id = parent_run_id or run.run_id
        run.start()
        self.runs[run.run_id] = run
        self.active_run_id = run.run_id
        return run

    def begin_continuation_run(self, goal: str, *, source: str) -> RunRecord:
        """Continue a completed Run without reopening its terminal record."""
        parent = self.active_run()
        if parent is None:
            raise ValueError("runtime continuation requires an existing Run")
        if parent.status not in TERMINAL_RUN_STATUSES:
            raise ValueError("runtime continuation requires a terminal parent Run")
        run = self.begin_run(goal, source=source, parent_run_id=parent.run_id)
        run.root_run_id = parent.root_run_id or parent.run_id
        run.model_config = dict(parent.model_config)
        run.plan = self.plan_manager.snapshot()
        return run

    def active_run(self) -> RunRecord | None:
        return self.runs.get(self.active_run_id or "")

    def current_run(self) -> RunRecord | None:
        """Return the active run, or the most recently created run for history views."""
        return self.active_run() or next(reversed(self.runs.values()), None)

    def current_run_status(self) -> RunStatus | Literal["idle"]:
        run = self.current_run()
        return run.status if run is not None else "idle"

    def current_goal(self) -> str:
        """Project the current/most-recent Run goal without duplicating it."""
        run = self.current_run()
        if run is not None and run.source == "runtime_event":
            root = self.runs.get(run.root_run_id)
            if root is not None:
                return root.goal
        return run.goal if run is not None else self.session_label

    def _next_message_id(self) -> MessageId:
        self.message_id_counter += 1
        return f"msg_{self.message_id_counter}"

    @property
    def messages(self) -> tuple[dict[str, Any], ...]:
        """只读兼容视图:外部不能靠 append 绕过 Session.append_message。"""
        return tuple(self.wire_messages())

    def wire_messages(self) -> list[dict[str, Any]]:
        """Compatibility projection for text-only Chat Completions callers.

        Provider adapters consume :meth:`conversation_messages` so internal
        attachment references and Responses state never leak to a wire request.
        """
        return [
            deepcopy({
                key: value
                for key, value in record.message.items()
                if key not in {"parts", "attachments", "provider_state"}
            })
            for record in self.message_records
        ]

    def conversation_messages(self) -> list[dict[str, Any]]:
        """Return provider-neutral message records without mutable internals."""
        return [deepcopy(record.message) for record in self.message_records]

    def append_message(
        self, message: dict[str, Any], *, source: str | None = None,
    ) -> MessageId:
        """把一条消息落进 wire 记录,同步累加 running total,返回稳定 id。

        这是 wire 的【唯一追加入口】:context_tokens 要准,就不能让任何人绕过它
        直接改 message_records。追加时用估算累加;assistant 那条的估算会在
        record_usage_for_turn 里被 prompt+completion 的估算锚点覆盖掉。
        """
        message_id = self._next_message_id()
        durable_message = deepcopy(message)
        message_source = source or {
            "user": "user_input",
            "assistant": "model_output",
            "tool": "tool_result",
            "system": "system_instruction",
        }.get(str(durable_message.get("role", "")), "system_feedback")
        self.message_records.append(MessageRecord(
            id=message_id, message=durable_message, source=message_source,
        ))
        self.context_tokens += estimate_message_tokens(durable_message)
        return message_id

    def append_user_message(
        self, prompt: str, attachment_ids: list[str] | tuple[str, ...] = (),
    ) -> MessageId:
        """Append a normalized user message with optional durable image parts."""
        turn = UserTurnInput(prompt=prompt, attachment_ids=tuple(attachment_ids))
        if not turn.prompt.strip() and not turn.attachment_ids:
            raise ValueError("a user message needs text or an attachment")
        unknown = [item for item in turn.attachment_ids if item not in self.attachments]
        if unknown:
            raise ValueError("unknown attachment id")
        parts = ([TextPart(turn.prompt)] if turn.prompt else []) + [
            ImagePart(attachment_id) for attachment_id in turn.attachment_ids
        ]
        return self.append_message(ConversationMessage("user", parts).to_dict())  # type: ignore[arg-type]

    def attachment_records(
        self, attachment_ids: list[str] | tuple[str, ...],
    ) -> list[AttachmentRecord]:
        records = [self.attachments.get(item) for item in attachment_ids]
        if any(item is None for item in records):
            raise ValueError("unknown attachment id")
        return [item for item in records if item is not None]

    def _append_assistant_message(self, content: str) -> MessageId:
        """把这轮 assistant 原文落进 wire 记录,返回它的稳定 id。

        turns 只存 message_id 来【引用】原文,不再复制一份字符串:
        wire 内容唯一存放在 MessageRecord.message,turns 是按 id 贴在上面的注解层。
        compaction 可以改写/删除/合并非 assistant 记录,但被 turn 引用的
        assistant 记录必须保留,除非未来把 assistant 原文归档到 TurnRecord。
        """
        return self.append_message({"role": "assistant", "content": content})

    def assistant_raw(self, turn: TurnRecord) -> str:
        """按 turn 记的 message_id 取回这轮 assistant 原文。"""
        for record in self.message_records:
            if record.id == turn.message_id:
                content = record.message.get("content")
                return content if isinstance(content, str) else ""
        raise KeyError(f"Assistant message not found: {turn.message_id}")

    def record_assistant_turn(
        self,
        assistant_raw: str,
        parsed: dict,
        route: Literal["tool_calls", "final"],
        tool_calls: list[ToolCall] | None = None,
        *,
        assistant_message: dict | None = None,
    ) -> TurnRecord:
        """记录一轮合法 assistant 输出。

        session 只记录已经解析好的事件,不负责解析 JSON。这样协议解析可以留在
        util/protocol 层,以后换输出协议时不会污染状态层。
        """
        tool_calls = tool_calls or []

        if route == "tool_calls" and not tool_calls:
            raise ValueError("route='tool_calls' 时必须提供 tool_calls")
        if route == "final" and tool_calls:
            raise ValueError("route='final' 时不能提供 tool_calls")

        tool_execution_ids: list[CallId] = []
        for tool_call in tool_calls:
            if not tool_call.id:
                raise ValueError("ToolCall 缺少 id,无法建立工具执行记录")
            if tool_call.id in self.tool_executions or tool_call.id in tool_execution_ids:
                raise ValueError(f"重复的 tool_call id: {tool_call.id}")

            tool_execution_ids.append(tool_call.id)

        # 校验全部通过后才落 wire / 改状态:
        # 上面任一 raise 都不能留下半截 message 或 tool_execution。
        step = self._next_step()
        if assistant_message is None:
            assistant_message = {"role": "assistant", "content": assistant_raw or None}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {"id": call.id, "type": "function", "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }} for call in tool_calls
                ]
        message_id = self.append_message(assistant_message)

        for tool_call in tool_calls:
            self.tool_executions[tool_call.id] = ToolExecutionRecord(
                call=tool_call,
                result=None,
                step=step,
                status="pending",
            )

        turn = TurnRecord(
            step=step,
            message_id=message_id,
            parsed=parsed,
            route=route,
            tool_execution_ids=tool_execution_ids,
            error=None,
        )
        self.turns.append(turn)
        run = self.active_run()
        if run is not None:
            turn.run_id = run.run_id
            turn.step_id = f"step_{uuid4().hex}"
            run.step_ids.append(turn.step_id)
            run.tool_execution_ids.extend(tool_execution_ids)
            for call_id in tool_execution_ids:
                self.tool_executions[call_id].run_id = run.run_id
                self.tool_executions[call_id].step_id = turn.step_id
        return turn

    def record_invalid_turn(
        self,
        assistant_raw: str,
        error: str,
        parsed: dict | None = None,
    ) -> TurnRecord:
        """记录一轮无效 assistant 输出,比如 JSON 解析失败或 route 失败。"""
        step = self._next_step()
        message_id = self._append_assistant_message(assistant_raw)
        turn = TurnRecord(
            step=step,
            message_id=message_id,
            parsed=parsed or {},
            route="invalid",
            tool_execution_ids=[],
            error=error,
        )
        self.turns.append(turn)
        run = self.active_run()
        if run is not None:
            turn.run_id, turn.step_id = run.run_id, f"step_{uuid4().hex}"
            run.step_ids.append(turn.step_id)
        return turn

    def record_tool_execution(
        self,
        call_id: CallId,
        result: ToolResult,
        status: ToolExecutionTerminal | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ToolExecutionRecord:
        """把某个 tool_call 的执行结果写回全局索引。"""
        execution = self.tool_executions.get(call_id)
        if execution is None:
            raise KeyError(f"Unknown tool_call id: {call_id}")

        execution.result = result
        execution.status = status or ("succeeded" if result.ok else "failed")
        if started_at is not None:
            execution.started_at = started_at
        if ended_at is not None:
            execution.ended_at = ended_at

        return execution

    def record_usage_for_turn(self, turn: TurnRecord, usage: UsageRecord) -> None:
        turn.usage = usage
        self.last_usage = usage
        # Provider usage is accounting only.  Keep the durable-history estimate
        # separate: it cannot be inferred from a provider's current request.
        self.context_tokens = sum(
            estimate_message_tokens(record.message)
            for record in self.message_records
        )
        self.add_usage(usage)
        run = self.active_run()
        if run is not None:
            for name, value in vars(usage).items():
                run.usage[name] = run.usage.get(name, 0) + value

    def add_usage(self, usage: UsageRecord) -> None:
        """累计消费；辅助请求不改变主对话的上下文估算。"""
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.total_tokens += usage.total_tokens

    def task_usage(self) -> UsageRecord:
        usage = UsageRecord(**{
            name: max(0, value - getattr(self.task_usage_start, name))
            for name, value in vars(self.total_usage).items()
        })
        if self.agent_task_id is None:
            for task in self.control_plane.snapshot()["tasks"]:
                if task["root_turn_id"] == self.agent_root_turn_id:
                    for name, value in task["usage"].items():
                        setattr(usage, name, getattr(usage, name) + value)
        return usage

    def record_verification(
        self,
        turn: TurnRecord,
        approved: bool,
        issues: list[dict[str, str]],
    ) -> VerificationRecord:
        if turn not in self.turns or turn.route != "final":
            raise ValueError("verification 只能关联已记录的 final turn")
        record = VerificationRecord(approved=approved, issues=issues)
        turn.verification = record
        return record

    def mark_completed(self) -> None:
        turn_id = self.agent_root_turn_id
        if turn_id and turn_id not in self.committed_turn_ids:
            self.committed_turn_ids.append(turn_id)
            self.committed_turn_ids = self.committed_turn_ids[-1_000:]
        run = self.active_run()
        if run is not None:
            run.plan = self.plan_manager.snapshot()
            run.finish("completed")

    def is_turn_committed(self, turn_id: str | None = None) -> bool:
        candidate = self.agent_root_turn_id if turn_id is None else turn_id
        return bool(candidate and candidate in self.committed_turn_ids)

    def revoke_turn_commit(self, turn_id: str | None = None) -> None:
        candidate = self.agent_root_turn_id if turn_id is None else turn_id
        if candidate:
            self.committed_turn_ids = [
                item for item in self.committed_turn_ids if item != candidate
            ]

    def mark_max_steps(self) -> None:
        if self.active_run() is not None:
            self.active_run().finish("failed", error="max steps reached")

    def mark_failed(self) -> None:
        if self.active_run() is not None:
            self.active_run().finish("failed")

    def mark_cancelled(self) -> None:
        if self.active_run() is not None:
            self.active_run().finish("cancelled", error="cancellation requested")


@dataclass
class MessageRecord:
    id: MessageId
    message: dict[str, Any]
    source: str = "system_feedback"


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
class VerificationRecord:
    approved: bool
    issues: list[dict[str, str]]


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
