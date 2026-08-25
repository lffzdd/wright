"""Unified task facade over the runtime-specific task implementations."""

from .service import (
    AgentTaskBackend,
    ShellTaskBackend,
    TaskBackend,
    TaskNotFoundError,
    TaskService,
    TaskWaitCancelled,
)
from .types import (
    RuntimeTask,
    TaskKind,
    TaskStatus,
    TERMINAL_TASK_STATUSES,
)

__all__ = [
    "AgentTaskBackend",
    "RuntimeTask",
    "ShellTaskBackend",
    "TaskBackend",
    "TaskKind",
    "TaskNotFoundError",
    "TaskService",
    "TaskStatus",
    "TaskWaitCancelled",
    "TERMINAL_TASK_STATUSES",
]
