from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal

from ..permission import PermissionCheckResult
from ..processes import RuntimeResources
from ..tool_capabilities import ToolCapabilities, assemble_tool_capabilities

TimeoutOwner = Literal["executor", "tool"]


class ToolCancelledError(RuntimeError):
    """工具观察到取消信号后主动退出。"""


def _not_concurrency_safe(args: dict[str, Any]) -> bool:
    """新工具默认排他执行；必须显式声明才允许进入并发批。"""
    return False


@dataclass
class ToolRuntime:
    """执行器传给工具的运行期上下文,不属于模型可见参数。

    只放"程序运行时能力":渲染/进度回调、调用标识、workspace/cwd、取消信号等。
    不放模型生成的业务参数(command/file/timeout...),那些只走 ToolCall.arguments。
    """

    # 当前工具调用标识:用于日志、进度事件、后台任务关联。
    tool_name: str = ""
    tool_call_id: str = ""

    # Explicit, bounded domain operations assembled by ToolExecutor.  Tools
    # never receive Session, RunStore, or RuntimeServices.
    capabilities: ToolCapabilities | None = None
    # Owns process handles, locks, threads and streaming projections.  It is
    # intentionally separate from checkpointed Session data.
    runtime_resources: RuntimeResources | None = None
    # Session-scoped lifecycle bus. Kept process-local and intentionally absent
    # from tool schemas/checkpoints.
    lifecycle: Any = None
    # Process-local bag shared across replace()d per-call runtimes.
    # Not in schemas or checkpoints. Tools store their own records here.
    scratch_lock: RLock = field(default_factory=RLock, repr=False)
    scratch: dict[str, Any] = field(default_factory=dict)

    # 文本流式输出:例如 shell stdout。命名保持通用,不绑定 command 工具。
    emit_output: Callable[[str], None] | None = None

    # 结构化进度事件:未来可用于下载进度、批处理进度、后台任务状态等。
    emit_progress: Callable[[dict[str, Any]], None] | None = None

    # Shell 后台任务完成时主动通知主循环。task_id 作为参数传入回调，主循环
    # 再通过 TaskService 解析统一 RuntimeTask。None 则不通知。
    notify_background_done: Callable[[str], None] | None = None

    # 每次调用独立的取消信号。工具里的长循环/阻塞分段应定期检查。
    cancellation_check: Callable[[], bool] | None = None
    cancellation_reason: Callable[[], str] | None = None
    # 子 Agent 生命周期结束后不能留下无人管理的后台进程。
    allow_background_tasks: bool = True

    def __post_init__(self) -> None:
        if self.runtime_resources is None and self.capabilities is not None:
            session_id = self.capabilities.scope.session_id
            if session_id:
                self.runtime_resources = RuntimeResources.for_session(session_id)

    def is_cancelled(self) -> bool:
        return bool(self.cancellation_check and self.cancellation_check())

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise ToolCancelledError(
                f"{self.tool_name or 'tool'} cancelled"
            )

    def get_cancellation_reason(self) -> str:
        return self.cancellation_reason() if self.cancellation_reason else ""


def tool_runtime_for_session(
    session,
    *,
    services=None,
    runtime_resources: RuntimeResources | None = None,
    workspace_dir=None,
    cwd_provider=None,
    **kwargs,
) -> ToolRuntime:
    """Build a tool runtime from explicit, bounded capabilities for tests/adapters.

    The Session is consumed at this composition boundary and is not retained by
    :class:`ToolRuntime`.
    """
    capabilities, resources = assemble_tool_capabilities(
        session, services, runtime_resources,
        workspace_dir=workspace_dir, cwd_provider=cwd_provider,
    )
    return ToolRuntime(
        capabilities=capabilities,
        runtime_resources=resources,
        **kwargs,
    )


def _default_check_permission(
    args: dict[str, Any], runtime: ToolRuntime
) -> PermissionCheckResult:
    return PermissionCheckResult(
        "allow",
        f"{runtime.tool_name or 'tool'}: allowed by default tool permission",
        source="tool_default",
    )


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    call: Callable[[dict[str, Any], ToolRuntime], "ToolResult"]
    check_permission: Callable[[dict[str, Any], ToolRuntime], PermissionCheckResult] = (
        _default_check_permission
    )
    is_concurrency_safe: Callable[[dict[str, Any]], bool] = _not_concurrency_safe
    # 这类工具不能被普通的 allow/bypass 规则直接放行。执行器会把它们交给
    # PermissionResolver 的 interaction_handler，由交互层回填已确认的 arguments
    # 后才调用 call()。ask_user 是当前唯一使用者，未来 TUI/Web 表单也可复用。
    requires_user_interaction: bool = False
    # executor:统一 deadline + 协作取消；tool:工具自己定义超时语义(如 shell 转后台)。
    timeout_owner: TimeoutOwner = "executor"
    # 可选的工具专属 executor deadline。None 使用 Agent 的通用 tool_timeout；
    # 子 Agent 这类长任务需要比普通文件/网络工具更长的独立预算。
    execution_timeout: float | None = None
    # Some internal tools remain executable without being sent to the model.
    expose_to_model: bool = True
    # Specialized tools stay executable but can be omitted from the baseline
    # schema payload until tool_search activates them for this Agent session.
    defer_to_model: bool = False
    # Descriptive metadata is registered once; executor still checks the
    # immutable Run capability snapshot before any side effect occurs.
    source: str = "builtin"
    effect: Literal["read", "write", "process", "network", "internal"] = "internal"

    def to_dict(self):
        # 并发与超时策略是系统调度元数据,不喂给模型。
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    media_type: str
    name: str
    size: int
    run_id: str = ""
    call_id: str = ""
    storage_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "media_type": self.media_type, "name": self.name,
            "size": self.size, "run_id": self.run_id, "call_id": self.call_id,
            "storage_path": self.storage_path,
        }


def split_tool_catalog(tools: Sequence[Tool]) -> tuple[list[str], list[str]]:
    """Baseline vs deferred names, in assembly order."""
    baseline: list[str] = []
    deferred: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        if not tool.expose_to_model or tool.name in seen:
            continue
        seen.add(tool.name)
        if tool.defer_to_model:
            deferred.append(tool.name)
        else:
            baseline.append(tool.name)
    return baseline, deferred


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class ToolResult:
    ok: bool
    err: str = ""
    data: Any = None
    summary: str = ""
    content: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()

    @classmethod
    def success(cls, data=None, *, summary: str = "", content=(), artifacts=()) -> "ToolResult":
        return cls(True, "", data, summary, tuple(content), tuple(artifacts))

    @classmethod
    def fail(cls, err: str, data=None, *, summary: str = "", content=(), artifacts=()) -> "ToolResult":
        return cls(False, err, data, summary, tuple(content), tuple(artifacts))

    def to_dict(self):
        return {
            "ok": self.ok,
            "err": self.err,
            "data": self.data,
            "summary": self.summary,
            "content": [dict(item) for item in self.content],
            "artifacts": [item.to_dict() for item in self.artifacts],
        }
