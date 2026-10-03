"""Query and control tools for shell commands owned by this session."""

from __future__ import annotations

from ....application.execution.identity import (
    ExecutionKindMismatch,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)
from ....domain.model.tool import ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolCancelledError, ToolRuntime


def _commands(runtime: ToolRuntime | None):
    if runtime is None or runtime.capabilities is None or runtime.capabilities.commands is None:
        raise RuntimeError("command control requires command execution")
    return runtime.capabilities.commands


def get_command(command_id: str, offset: int = 0, limit: int = 8000, runtime: ToolRuntime | None = None) -> ToolResult:
    try:
        view = _commands(runtime).get(command_id, offset=offset, limit=limit)
    except (ExecutionNotFound, ExecutionKindMismatch, TypeError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success(view)


def wait_command(command_id: str, timeout: float = 60, runtime: ToolRuntime | None = None) -> ToolResult:
    try:
        view = _commands(runtime).wait(
            command_id,
            timeout=timeout,
            cancellation_check=runtime.is_cancelled if runtime else None,
        )
    except ExecutionWaitCancelled as exc:
        raise ToolCancelledError(str(exc)) from exc
    except (ExecutionNotFound, ExecutionKindMismatch, TypeError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success(view)


def terminate_command(
    command_id: str,
    reason: str = "terminated by tool",
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        view = _commands(runtime).terminate(command_id, reason)
    except (ExecutionNotFound, ExecutionKindMismatch) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success(view)


def list_commands(
    scope: str = "current_user_turn",
    status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        page = _commands(runtime).list(
            scope=scope, status=status, limit=limit, cursor=cursor
        )
    except (TypeError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success(page)


get_command_tool = Tool(
    name="get_command",
    description="Read one page of a shell command's output and its current status.",
    parameters={
        "type": "object",
        "properties": {
            "command_id": {"type": "string"},
            "offset": {"type": "integer", "default": 0, "minimum": 0},
            "limit": {"type": "integer", "default": 8000, "minimum": 1},
        },
        "required": ["command_id"],
    },
    call=lambda args, runtime: get_command(**args, runtime=runtime),
    required_capabilities=frozenset({"commands"}),
    access_descriptor=lambda _: ToolAccess.internal_read(),
)

wait_command_tool = Tool(
    name="wait_command",
    description="Wait until a shell command ends, or until timeout. Timeout does not terminate it.",
    parameters={
        "type": "object",
        "properties": {
            "command_id": {"type": "string"},
            "timeout": {"type": "number", "default": 60, "minimum": 0},
        },
        "required": ["command_id"],
    },
    call=lambda args, runtime: wait_command(**args, runtime=runtime),
    required_capabilities=frozenset({"commands"}),
    access_descriptor=lambda _: ToolAccess.internal_read(),
    timeout_owner="tool",
)

terminate_command_tool = Tool(
    name="terminate_command",
    description="Terminate a shell command and the process group this execution owns.",
    parameters={
        "type": "object",
        "properties": {"command_id": {"type": "string"}},
        "required": ["command_id"],
    },
    call=lambda args, runtime: terminate_command(**args, runtime=runtime),
    required_capabilities=frozenset({"commands"}),
    access_descriptor=lambda _: ToolAccess(frozenset({"execution_control"})),
)

list_commands_tool = Tool(
    name="list_commands",
    description=(
        "List shell commands for this session. scope=current_user_turn is the default; "
        "scope=session includes earlier turns. Use cursor to continue past the page."
    ),
    parameters={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["current_user_turn", "session"],
                "default": "current_user_turn",
            },
            "status": {"type": "string"},
            "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 100},
            "cursor": {"type": "string"},
        },
    },
    call=lambda args, runtime: list_commands(**args, runtime=runtime),
    required_capabilities=frozenset({"commands"}),
    access_descriptor=lambda _: ToolAccess.internal_read(),
)

command_tools = [
    get_command_tool,
    wait_command_tool,
    terminate_command_tool,
    list_commands_tool,
]

__all__ = [
    "get_command",
    "get_command_tool",
    "list_commands",
    "list_commands_tool",
    "terminate_command",
    "terminate_command_tool",
    "wait_command",
    "wait_command_tool",
]
