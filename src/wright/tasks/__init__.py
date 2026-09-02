"""Unified task facade over the runtime-specific task implementations."""

from .service import (
    AgentTaskBackend,
    ShellTaskBackend,
    TaskBackend,
    TaskService,
)
from .types import (
    RuntimeTask,
    TaskKind,
    TaskNotFoundError,
    TaskStatus,
    TaskWaitCancelled,
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
