from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from ..permission import PermissionCheckResult

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

    # 当前会话的本地执行边界。工具需要定位 workspace/cwd 时优先用这里,
    # 避免各工具自己猜 Path.cwd() 或维护重复状态。
    workspace_dir: Path | None = None
    cwd_provider: Callable[[], Path] | None = None
    session_state: Any = None
    # Session-scoped lifecycle bus. Kept process-local and intentionally absent
    # from tool schemas/checkpoints.
    lifecycle: Any = None

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

    def is_cancelled(self) -> bool:
        return bool(self.cancellation_check and self.cancellation_check())

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise ToolCancelledError(
                f"{self.tool_name or 'tool'} cancelled"
            )

    def get_cancellation_reason(self) -> str:
        return self.cancellation_reason() if self.cancellation_reason else ""


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
    # Compatibility aliases may remain executable for resumed/old transcripts
    # without teaching new model turns a duplicate API surface.
    expose_to_model: bool = True

    def to_dict(self):
        # 并发与超时策略是系统调度元数据,不喂给模型。
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


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

    @classmethod
    def success(cls, data=None) -> "ToolResult":
        return cls(ok=True, err="", data=data)

    @classmethod
    def fail(cls, err: str, data=None) -> "ToolResult":
        return cls(ok=False, err=err, data=data)

    def to_dict(self):
        return {
            "ok": self.ok,
            "err": self.err,
            "data": self.data,
        }
