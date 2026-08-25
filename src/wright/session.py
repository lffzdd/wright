from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any, Callable, Literal, TypeAlias
from uuid import uuid4

from openai.types.chat import ChatCompletionMessageParam

from .coordination import AgentControlPlane
from .planning import PlanManager
from .tools.base import ToolResult, ToolCall
from .util import estimate_message_tokens

CallId: TypeAlias = str
MessageId: TypeAlias = str
SessionStatus = Literal["running", "completed", "failed", "max_steps"]
TurnRoute = Literal["tool_calls", "final", "invalid"]
ToolExecutionTerminal = Literal["succeeded", "failed", "timeout"]
ToolExecutionStatus = Literal["pending", "running"] | ToolExecutionTerminal


@dataclass
class SessionState:
    session_id: str
    status: SessionStatus
    user_goal: str

    workspace_dir: Path
    cwd: Path

    turns: list[TurnRecord]
    # wire 内容唯一存放在 MessageRecord.message;turns 只用稳定 id 贴注解,
    # 不依赖 message_records 的当前位置。
    message_records: list[MessageRecord]

    tool_executions: dict[CallId, ToolExecutionRecord]
    background_tasks: dict[str, BackgroundTask]
    plan_manager: PlanManager
    # 目录只往 transcript 写一次。正文走 skill 工具的 tool_result，不另建激活表。
    skill_catalog_sent: bool = False
    # Root 与所有子 Agent 共享同一个控制面；子 session 用 agent_task_id
    # 标记自己在任务树中的位置，root 则为 None。
    control_plane: AgentControlPlane = field(default_factory=AgentControlPlane)
    agent_task_id: str | None = None
    agent_root_turn_id: str = ""
    # 当前 user turn 从哪个全局 step 之后开始。Verifier 用它隔离多轮 REPL 中
    # 旧任务的工具证据；checkpoint/resume 也靠它恢复本轮边界。
    active_turn_start_step: int = 0
    active_turn_start_message_index: int = 0
    last_usage: UsageRecord | None = None
    total_usage: UsageRecord = field(default_factory=lambda: UsageRecord())

    # 当前 messages 的预测 token 数(= 下次发送会有多大)。增量维护:追加时加、
    # 折叠时减;每轮拿到 usage 后用 prompt+completion 校准回服务端真值
    # (见 record_usage_for_turn),估算误差从不累积超过一轮的工具结果尾巴。
    context_tokens: int = 0

    step_count: int = 0
    max_steps: int = 25
    message_id_counter: int = 0
    _cwd_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    _background_tasks_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    # Live threads and queues are process-local; checkpoints intentionally do
    # not serialize them.  A restored session therefore treats live work as
    # unknown via the existing control-plane recovery path.
    agent_background_runtime: Any = field(default=None, repr=False, compare=False)
    durable_task_store: Any = field(default=None, repr=False, compare=False)
    autonomy_scheduler: Any = field(default=None, repr=False, compare=False)
    loop_registry: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def create(
        cls, user_goal: str, workspace_dir: Path, max_steps: int = 50
    ) -> SessionState:
        return cls(
            session_id=uuid4().hex[:6],
            status="running",
            user_goal=user_goal,
            workspace_dir=workspace_dir.resolve(),
            cwd=workspace_dir.resolve(),
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

    def register_background_task(self, task: "BackgroundTask") -> None:
        with self._background_tasks_lock:
            self.background_tasks[task.task_id] = task

    def get_background_task(self, task_id: str) -> "BackgroundTask | None":
        with self._background_tasks_lock:
            return self.background_tasks.get(task_id)

    def list_background_tasks(self) -> list["BackgroundTask"]:
        """Return a stable registry snapshot; individual tasks remain live."""
        with self._background_tasks_lock:
            return list(self.background_tasks.values())

    def _next_step(self) -> int:
        self.step_count += 1
        return self.step_count

    def mark_skill_catalog_sent(self) -> None:
        self.skill_catalog_sent = True

    def begin_user_turn(self, prompt: str) -> None:
        """记录当前任务目标及其证据边界。"""
        self.user_goal = prompt
        self.active_turn_start_step = self.step_count
        self.active_turn_start_message_index = len(self.message_records)
        if self.agent_task_id is None:
            self.agent_root_turn_id = (
                f"{self.session_id}:{self.active_turn_start_message_index}"
            )

    def _next_message_id(self) -> MessageId:
        self.message_id_counter += 1
        return f"msg_{self.message_id_counter}"

    @property
    def messages(self) -> tuple[ChatCompletionMessageParam, ...]:
        """只读兼容视图:外部不能靠 append 绕过 SessionState.append_message。"""
        return tuple(self.wire_messages())

    def wire_messages(self) -> list[ChatCompletionMessageParam]:
        """投影出发给 LLM 的纯 OpenAI wire messages,不携带内部 message_id。"""
        return [record.message for record in self.message_records]

    def append_message(self, message: ChatCompletionMessageParam) -> MessageId:
        """把一条消息落进 wire 记录,同步累加 running total,返回稳定 id。

        这是 wire 的【唯一追加入口】:context_tokens 要准,就不能让任何人绕过它
        直接改 message_records。追加时用估算累加;assistant 那条的估算会在
        record_usage_for_turn 里被 prompt+completion 精确校准覆盖掉。
        """
        message_id = self._next_message_id()
        self.message_records.append(MessageRecord(id=message_id, message=message))
        self.context_tokens += estimate_message_tokens(message)
        return message_id

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
        message_id = self._append_assistant_message(assistant_raw)

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
        return turn

    def record_invalid_turn(
        self,
        assistant_raw: str,
        error: str,
        parsed: dict | None = None,
    ) -> "TurnRecord":
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

    def record_usage_for_turn(self, turn: "TurnRecord", usage: "UsageRecord") -> None:
        turn.usage = usage
        self.last_usage = usage
        # 校准 running total:此刻 assistant 已入队、工具结果尚未追加,
        # prompt_tokens + completion_tokens 就是当前 wire 记录的精确大小——
        # 用它覆盖 context_tokens,一次性消灭之前累积的估算误差。
        self.context_tokens = usage.prompt_tokens + usage.completion_tokens
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.total_tokens += usage.total_tokens

    def record_verification(
        self,
        turn: "TurnRecord",
        approved: bool,
        issues: list[dict[str, str]],
    ) -> "VerificationRecord":
        if turn not in self.turns or turn.route != "final":
            raise ValueError("verification 只能关联已记录的 final turn")
        record = VerificationRecord(approved=approved, issues=issues)
        turn.verification = record
        return record

    def mark_running(self) -> None:
        # 多轮对话:新一轮开始时把上一轮留下的终态(completed/failed/max_steps)
        # 重置回 running,让 status 始终反映"当前这轮"而非历史。
        self.status = "running"

    def mark_completed(self) -> None:
        self.status = "completed"

    def mark_max_steps(self) -> None:
        self.status = "max_steps"

    def mark_failed(self) -> None:
        self.status = "failed"


@dataclass
class MessageRecord:
    id: MessageId
    message: ChatCompletionMessageParam


@dataclass
class ToolExecutionRecord:
    call: ToolCall
    result: ToolResult | None
    step: int
    status: ToolExecutionStatus
    started_at: float | None = None
    ended_at: float | None = None


@dataclass
class UsageRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: Any) -> "UsageRecord":
        """把 LLM 原始 usage 归一成 UsageRecord。

        原始 usage 形态不一:有的接口给 dict,有的给带属性的对象(SDK 模型),
        这种"形状差异"的知识收在这里,主循环不该操心。total 缺省时用
        prompt + completion 兜底。
        """
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = usage.get("total_tokens")
        else:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
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
    verification: "VerificationRecord | None" = None


@dataclass
class VerificationRecord:
    approved: bool
    issues: list[dict[str, str]]


@dataclass
class BackgroundTask:
    task_id: str
    process: Any
    output_lines: list[str]
    done: threading.Event
    output_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    # reader 线程完成时调用；由 execute_command 在注册时写入，主循环
    # 可借此收到统一的 TASK_DONE(task_id) 通知。
    on_done: Callable[[], None] | None = field(default=None, repr=False)
    # TaskService 需要的通用元数据。执行状态仍由 process + done 唯一决定，
    # 这些字段只补充描述、归属和取消意图，不形成另一套状态机。
    command: str = ""
    root_turn_id: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
