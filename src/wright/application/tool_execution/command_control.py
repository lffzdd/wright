"""Query and control tools for shell commands owned by this session."""

from __future__ import annotations

from ...domain.model.execution import (
    ExecutionKindMismatch,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)
from ...domain.model.tool import ToolAccess, ToolResult
from ...domain.model.tool_definition import Tool
from ...domain.model.tool_runtime import ToolCancelledError, ToolRuntime


def _commands(runtime: ToolRuntime | None):
    if runtime is None or runtime.capabilities is None or runtime.capabilities.commands is None:
        raise RuntimeError("command control requires command execution")
    return runtime.capabilities.commands


def shell_output(
    command_id: str,
    offset: int = 0,
    limit: int = 8000,
    timeout: float | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """Read one output page; with timeout, first wait up to that long for the end."""
    try:
        commands = _commands(runtime)
        if timeout is not None:
            view = commands.wait(
                command_id,
                timeout=timeout,
                offset=offset,
                limit=limit,
                cancellation_check=runtime.is_cancelled if runtime else None,
            )
        else:
            view = commands.get(command_id, offset=offset, limit=limit)
    except ExecutionWaitCancelled as exc:
        raise ToolCancelledError(str(exc)) from exc
    except (ExecutionNotFound, ExecutionKindMismatch, TypeError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success(view)


def shell_kill(
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


shell_output_tool = Tool(
    name="shell_output",
    description=(
        "Read one page of a background shell command's output and its status. "
        "Set timeout (seconds) to wait for it to end first; a timeout does not "
        "terminate it. Continue large output with offset=next_offset."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command_id": {"type": "string"},
            "offset": {"type": "integer", "default": 0, "minimum": 0},
            "limit": {"type": "integer", "default": 8000, "minimum": 1},
            "timeout": {"type": "number", "minimum": 0, "description": "Seconds to wait for the command to end before reading; omit to read now"},
        },
        "required": ["command_id"],
    },
    call=lambda args, runtime: shell_output(**args, runtime=runtime),
    required_capabilities=frozenset({"commands"}),
    access_descriptor=lambda _: ToolAccess.internal_read(),
    timeout_owner="tool",
)

shell_kill_tool = Tool(
    name="shell_kill",
    description="Terminate a shell command and the process group this execution owns.",
    parameters={
        "type": "object",
        "properties": {"command_id": {"type": "string"}},
        "required": ["command_id"],
    },
    call=lambda args, runtime: shell_kill(**args, runtime=runtime),
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
    defer_to_model=True,
)

command_tools = [
    shell_output_tool,
    shell_kill_tool,
    list_commands_tool,
]

__all__ = [
    "command_tools",
    "list_commands",
    "list_commands_tool",
    "shell_kill",
    "shell_kill_tool",
    "shell_output",
    "shell_output_tool",
]
