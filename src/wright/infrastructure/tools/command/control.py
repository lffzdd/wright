"""Root-only tools for one background command execution.

``command_id`` identifies that execution. Shell metadata and the process
registry remain the state owners. These tools do not accept agent or schedule ids.
"""

from __future__ import annotations

from typing import Any

from ....application.tasks.service import TaskService
from ....domain.model.tasks import (
    RuntimeTask,
    TaskKindMismatch,
    TaskNotFoundError,
    TaskWaitCancelled,
)
from ....domain.model.tool import ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime

_SHELL_STATUSES = ("running", "completed", "failed", "cancelled", "unknown")
_LIST_LIMIT = 100
_LIST_TEXT = 500


def command_execution_view(
    task: RuntimeTask, *, summary: bool = False
) -> dict[str, Any]:
    """Model view of one shell execution. Output is already capped by the backend."""
    text_limit = _LIST_TEXT if summary else 8_000
    description_limit = _LIST_TEXT if summary else 2_000
    output = task.output[-text_limit:]
    view: dict[str, Any] = {
        "command_id": task.id,
        "status": task.status,
        "terminal": task.terminal,
        "description": task.description[:description_limit],
        "output": output,
        "error": task.error[:text_limit],
        "returncode": task.returncode,
        "cancel_requested": task.cancel_requested,
        "cancel_reason": task.cancel_reason[:text_limit],
        "created_at": task.created_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
    }
    if task.status == "unknown":
        view["outcome"] = "unconfirmed"
        if not summary:
            view["note"] = (
                "Status cannot be confirmed. This is not a successful completion."
            )
    elif task.terminal:
        view["outcome"] = task.status
    else:
        view["outcome"] = "waiting"
    if task.cancel_requested and task.status == "running":
        view["message"] = (
            "Termination was requested. The command has not exited yet."
        )
    return view


def _service(runtime: ToolRuntime) -> TaskService:
    service = runtime.capabilities.tasks if runtime.capabilities else None
    if service is None:
        raise RuntimeError("command tool requires task classification")
    return service


def _observe(exc: Exception, *, command_id: str) -> ToolResult:
    if isinstance(exc, TaskKindMismatch):
        return ToolResult.fail(str(exc))
    if isinstance(exc, TaskNotFoundError):
        return ToolResult.fail(f"Unknown command_id: {command_id}")
    if isinstance(exc, TaskWaitCancelled):
        return ToolResult.fail(
            "This wait was cancelled. The command was not terminated."
        )
    return ToolResult.fail(str(exc))


def get_command(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    command_id = str(arguments["command_id"])
    try:
        task = _service(runtime).read_kind(command_id, "shell")
    except (RuntimeError, TaskKindMismatch, TaskNotFoundError) as exc:
        return _observe(exc, command_id=command_id)
    return ToolResult.success(command_execution_view(task))


def wait_command(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    command_id = str(arguments["command_id"])
    timeout = float(arguments.get("timeout", 30))
    try:
        task = _service(runtime).wait_kind(
            command_id,
            "shell",
            timeout=timeout,
            cancellation_check=runtime.is_cancelled,
        )
    except (
        RuntimeError, TaskKindMismatch, TaskNotFoundError, TaskWaitCancelled, ValueError
    ) as exc:
        return _observe(exc, command_id=command_id)
    data = command_execution_view(task)
    data["wait_completed"] = task.terminal
    data["wait_timed_out"] = not task.terminal
    return ToolResult.success(data)


def terminate_command(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    command_id = str(arguments["command_id"])
    reason = str(arguments.get("reason") or "Root Agent terminated the command")[:1_000]
    try:
        service = _service(runtime)
        before = service.read_kind(command_id, "shell")
        task = service.cancel_kind(command_id, "shell", reason=reason)
    except (RuntimeError, TaskKindMismatch, TaskNotFoundError) as exc:
        return _observe(exc, command_id=command_id)
    data = command_execution_view(task)
    data["already_terminal"] = before.terminal
    return ToolResult.success(data)


def list_commands(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    include_all = bool(arguments.get("include_all_turns", False))
    status = arguments.get("status")
    if status is not None and status not in _SHELL_STATUSES:
        return ToolResult.fail(f"unsupported command status: {status}")
    try:
        scope = runtime.capabilities.scope if runtime.capabilities else None
        root_turn_id = None if include_all or scope is None else scope.root_turn_id
        tasks = _service(runtime).list(
            kind="shell",
            status=status if isinstance(status, str) else None,
            root_turn_id=root_turn_id,
        )
    except (RuntimeError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    bounded = tasks[:_LIST_LIMIT]
    return ToolResult.success({
        "count": len(bounded),
        "truncated": len(tasks) > len(bounded),
        "scope": "session" if include_all else "current_user_turn",
        "commands": [command_execution_view(task, summary=True) for task in bounded],
    })


def _describe_read(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("command_id") or arguments.get("status") or ""),
        reason="observe a command execution",
    )


def _describe_terminate(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"execution_control"}),
        subject=str(arguments.get("command_id") or ""),
        risk_flags=("controls_live_execution",),
        reason="terminate a command process tree",
    )


_COMMAND_ID = {
    "type": "string",
    "minLength": 1,
    "description": "Identifier of one command execution returned by execute_command.",
}


get_command_tool = Tool(
    name="get_command",
    description=(
        "Read one background command by command_id. Returns output, exit code, "
        "status, and cancellation state. outcome=unconfirmed means the process "
        "state cannot be confirmed and is not a successful completion."
    ),
    parameters={
        "type": "object",
        "properties": {"command_id": _COMMAND_ID},
        "required": ["command_id"],
        "additionalProperties": False,
    },
    call=get_command,
    access_descriptor=_describe_read,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: True,
)


wait_command_tool = Tool(
    name="wait_command",
    description=(
        "Wait up to timeout seconds for one command execution. A timeout is an "
        "observation with wait_timed_out=true; it does not terminate the command "
        "and does not mean the command failed. If this wait is itself cancelled, "
        "the command is left running."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command_id": _COMMAND_ID,
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": 300,
                "default": 30,
            },
        },
        "required": ["command_id"],
        "additionalProperties": False,
    },
    call=wait_command,
    access_descriptor=_describe_read,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: True,
    timeout_owner="tool",
)


terminate_command_tool = Tool(
    name="terminate_command",
    description=(
        "Terminate one command execution and its process tree, not only the parent "
        "process. An already finished command is left unchanged. A command whose "
        "process state is unknown is not reported as terminated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command_id": _COMMAND_ID,
            "reason": {"type": "string", "maxLength": 1_000},
        },
        "required": ["command_id"],
        "additionalProperties": False,
    },
    call=terminate_command,
    access_descriptor=_describe_terminate,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: False,
)


list_commands_tool = Tool(
    name="list_commands",
    description=(
        "List background commands so you can recover a command_id, see what is "
        "still running, or restore context. By default only commands started "
        "during the current user turn are included. Set include_all_turns=true "
        "to include every background command in this session; other sessions are "
        "never included. Results are capped in count and output length. Filter "
        "by status when you only need one lifecycle state. unknown/unconfirmed "
        "is not a successful completion."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": list(_SHELL_STATUSES),
            },
            "include_all_turns": {
                "type": "boolean",
                "default": False,
                "description": (
                    "False lists only the current user turn. True lists this "
                    "session. Never lists another session."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    call=list_commands,
    access_descriptor=_describe_read,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: True,
)


command_tools = [
    get_command_tool,
    wait_command_tool,
    terminate_command_tool,
    list_commands_tool,
]
