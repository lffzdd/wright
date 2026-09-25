"""Runtime context supplied to one concrete tool invocation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from ...execution import AuthorizedExecution
from ...permission.scope import AccessScope
from ...processes import RuntimeResources
from .capabilities import ToolCapabilities


class ToolCancelledError(RuntimeError):
    """工具观察到取消信号后主动退出。"""


@dataclass
class ToolRuntime:
    """执行器传给具体工具的运行期上下文。"""

    tool_name: str = ""
    tool_call_id: str = ""
    capabilities: ToolCapabilities | None = None
    execution: AuthorizedExecution | None = None
    access_scope: AccessScope | None = None
    runtime_resources: RuntimeResources | None = None
    lifecycle: Any = None
    scratch_lock: RLock = field(default_factory=RLock, repr=False)
    scratch: dict[str, Any] = field(default_factory=dict)
    emit_output: Callable[[str], None] | None = None
    emit_progress: Callable[[dict[str, Any]], None] | None = None
    notify_background_done: Callable[[str], None] | None = None
    cancellation_check: Callable[[], bool] | None = None
    cancellation_reason: Callable[[], str] | None = None
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
            raise ToolCancelledError(f"{self.tool_name or 'tool'} cancelled")

    def get_cancellation_reason(self) -> str:
        return self.cancellation_reason() if self.cancellation_reason else ""
