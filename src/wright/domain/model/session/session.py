"""Session aggregate root for Wright agent executions."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from ....utils.token_counter import estimate_message_tokens
from ..agent.control import AgentControlPlane
from ..attachment import AttachmentRecord
from ..command.record import CommandRecord
from ..llm.usage import UsageRecord
from ..planning import PlanManager
from ..tool import ToolCall, ToolResult
from .conversation import (
    ConversationMessage,
    ImagePart,
    MessageId,
    MessageRecord,
    TextPart,
    UserTurnInput,
)
from .environment import ExecutionEnvironment
from .records import (
    CallId,
    SessionLifecycle,
    ToolExecutionRecord,
    ToolExecutionTerminal,
    TurnRecord,
    VerificationRecord,
)
from .run import TERMINAL_RUN_STATUSES, RunRecord, RunStatus, new_run_id


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
    commands: dict[str, CommandRecord]
    plan_manager: PlanManager
    # A stable label for the long-lived conversation, not the current task.
    # Every executable goal belongs to its RunRecord.
    session_label: str = ""
    lifecycle: SessionLifecycle = "open"
    # Durable state is grouped by project_root. workspace_dir remains the
    # actual execution directory for compatibility with existing tools.
    additional_working_directories: list[Path] = field(default_factory=list)
    # Structured allow rules for this session only. They are not stored on a
    # shared policy object, so a child session cannot widen its parent.
    permission_rules: list[dict] = field(default_factory=list)
    project_root: Path | None = None
    environment: ExecutionEnvironment = "local"
    base_commit: str | None = None
    branch_name: str | None = None
    # Authoritative per-execution state.  Legacy fields below are projections
    # retained while Agent/Tool APIs are migrated.
    runs: dict[str, RunRecord] = field(default_factory=dict)
    active_run_id: str | None = None
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
    # Episode snapshots waiting for background agents. Missing on old checkpoints.
    pending_episode_finalizes: list[dict[str, Any]] = field(default_factory=list)
    # Extraction decisions keyed by root_run_id. A stored decision is not retried.
    # A crash before this field is checkpointed can call the model again.
    semantic_extract_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 当前 user turn 从哪个全局 step 之后开始。Verifier 用它隔离多轮 REPL 中
    # 旧任务的工具证据；checkpoint/resume 也靠它恢复本轮边界。
    active_turn_start_step: int = 0
    active_turn_start_message_index: int = 0
    last_usage: UsageRecord | None = None
    total_usage: UsageRecord = field(default_factory=lambda: UsageRecord())

    task_usage_start: UsageRecord = field(default_factory=lambda: UsageRecord())

    # The active main-model choice is session state so /model survives resume.
    model_name: str | None = None
    # Execution ceilings. None permission_mode follows the settings object.
    # A stored mode is frozen for this session and does not track later edits
    # of the user defaults while a turn is already authorized.
    interaction_mode: Literal["agent", "plan", "ask"] = "agent"
    permission_mode: str | None = None
    # Tokenizer estimate of the last assembled model request. Not billing usage.
    request_context_breakdown: dict[str, Any] = field(default_factory=dict)
    # Non-image uploads. Bytes stay outside the edited workspace.
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    _commands_lock: threading.RLock = field(
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
        additional_working_directories: list[Path] | None = None,
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
            additional_working_directories=[
                Path(item).expanduser().absolute()
                for item in (additional_working_directories or ())
            ],
            turns=[],
            message_records=[],
            tool_executions={},
            commands={},
            plan_manager=PlanManager(),
            max_steps=max_steps,
        )

    def get_cwd(self) -> Path:
        with self._cwd_lock:
            return self.cwd

    def set_cwd(self, cwd: Path) -> None:
        if not isinstance(cwd, Path):
            raise TypeError("Session.set_cwd requires pathlib.Path")
        with self._cwd_lock:
            self.cwd = cwd.resolve()

    def add_working_directory(self, directory: Path) -> Path:
        """Grant an extra working-directory root for this session."""
        from ...policy.permission.scope import is_under, resolve_root

        resolved = resolve_root(directory)
        origin = resolve_root(self.workspace_dir)
        with self._cwd_lock:
            if resolved == origin or is_under(resolved, origin):
                return resolved
            if any(
                item == resolved or is_under(resolved, item)
                for item in self.additional_working_directories
            ):
                return resolved
            self.additional_working_directories.append(resolved)
            return resolved

    def remove_working_directory(self, directory: Path) -> bool:
        """Drop one extra session root. The execution root is not in this list."""

        from ...policy.permission.scope import resolve_root

        resolved = resolve_root(directory)
        with self._cwd_lock:
            remaining = [
                item for item in self.additional_working_directories if item != resolved
            ]
            changed = len(remaining) != len(self.additional_working_directories)
            self.additional_working_directories = remaining
            return changed

    def working_directories_snapshot(self) -> tuple[Path, ...]:
        """Return the current session roots without exposing mutable state."""
        with self._cwd_lock:
            return tuple(self.additional_working_directories)

    def add_permission_rule(self, rule: dict) -> None:
        """Remember one structured allow rule on this session."""
        if not isinstance(rule, dict):
            raise TypeError("permission rule must be a structured record")
        record = dict(rule)
        with self._cwd_lock:
            if record not in self.permission_rules:
                self.permission_rules.append(record)

    def remove_permission_rule(self, rule: dict) -> bool:
        """Drop one session allow rule. Returns whether the list changed."""
        if not isinstance(rule, dict):
            raise TypeError("permission rule must be a structured record")
        record = dict(rule)
        with self._cwd_lock:
            if record not in self.permission_rules:
                return False
            self.permission_rules = [
                item for item in self.permission_rules if item != record
            ]
            return True

    def restore_authorization(
        self,
        directories: tuple[Path, ...] | list[Path],
        rules: list[dict],
    ) -> None:
        """Put session grants back after a failed authorization commit."""
        with self._cwd_lock:
            self.additional_working_directories = list(directories)
            self.permission_rules = [dict(item) for item in rules]

    def access_scope(self):
        """Build an immutable permission snapshot at a composition boundary."""
        from ...policy.permission.scope import AccessScope

        return AccessScope(self.workspace_dir, self.working_directories_snapshot())

    def register_command(self, record: CommandRecord) -> None:
        """Store command metadata. The command runtime owns process handles."""
        with self._commands_lock:
            self.commands[record.command_id] = record

    def get_command(self, command_id: str) -> CommandRecord | None:
        with self._commands_lock:
            return self.commands.get(command_id)

    def list_commands(self) -> list[CommandRecord]:
        """Return command metadata without output logs or process handles."""
        with self._commands_lock:
            return list(self.commands.values())

    def mark_commands_cancel_requested(
        self, command_ids: tuple[str, ...], reason: str
    ) -> None:
        """Reflect a runtime shutdown in command metadata."""
        with self._commands_lock:
            for command_id in command_ids:
                record = self.commands.get(command_id)
                if record is not None and record.disposition == "running":
                    record.cancel_requested = True
                    record.cancel_reason = reason[:1_000]

    def _next_step(self) -> int:
        self.step_count += 1
        return self.step_count

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


