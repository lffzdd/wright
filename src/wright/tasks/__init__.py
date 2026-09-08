"""Unified task facade over the runtime-specific task implementations."""

from .service import (
    AgentTaskBackend,
    ShellTaskBackend,
    TaskBackend,
    TaskService,
)
from .types import (
    TERMINAL_TASK_STATUSES,
    RuntimeTask,
    TaskKind,
    TaskNotFoundError,
    TaskStatus,
    TaskWaitCancelled,
)

__all__ = [
    "TERMINAL_TASK_STATUSES",
    "AgentTaskBackend",
    "RuntimeTask",
    "ShellTaskBackend",
    "TaskBackend",
    "TaskKind",
    "TaskNotFoundError",
    "TaskService",
    "TaskStatus",
    "TaskWaitCancelled",
]
