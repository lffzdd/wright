"""execute_command tool. Lifecycle belongs to CommandExecution."""

from __future__ import annotations

from ....application.command.execution import CommandOutcome
from ....domain.model.tool import ToolResult
from ..base import Tool
from ..runtime import ToolCancelledError, ToolRuntime
from .permissions import (
    describe_execute_command_access,
    is_execute_command_concurrency_safe,
)


def execute_command(
    command: str,
    timeout: int = 20,
    run_in_background: bool = False,
    network: bool = False,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """执行 shell 命令。启动、输出、后台转换和清理由命令运行时负责。"""
    del network  # The immutable grant alone controls networking.
    capabilities = runtime.capabilities if runtime is not None else None
    commands = capabilities.commands if capabilities is not None else None
    if runtime is None or commands is None or capabilities is None:
        return ToolResult.fail("command tool requires command execution")
    if runtime.execution is None:
        return ToolResult.fail("command tool requires an invocation authorization")
    journal = getattr(runtime, "file_journal", None)
    before = journal.capture() if journal is not None else None
    outcome = commands.execute(
        command=command,
        timeout=timeout,
        run_in_background=run_in_background,
        execution=runtime.execution,
        allow_background=runtime.allow_background_tasks,
        emit_output=runtime.emit_output,
        is_cancelled=runtime.is_cancelled,
        root_turn_id=capabilities.scope.root_turn_id,
        run_id=capabilities.scope.run_id,
        access_scope=runtime.access_scope,
        set_cwd=capabilities.set_cwd,
    )
    if journal is not None and before is not None:
        data = outcome.data or {}
        if "returncode" in data:
            try:
                journal.commit_capture(
                    before,
                    call_id=getattr(runtime, "tool_call_id", ""),
                    tool_name="execute_command",
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception("change journal missed a command")
        elif data.get("command_id"):
            journal.hold(str(data["command_id"]), before, call_id=getattr(runtime, "tool_call_id", ""))
    return _project(outcome)


def _project(outcome: CommandOutcome) -> ToolResult:
    if outcome.cancelled:
        raise ToolCancelledError("execute_command cancelled")
    if outcome.ok:
        return ToolResult.success(outcome.data or {})
    return ToolResult.fail(outcome.error, data=outcome.data)


execute_command_tool = Tool(
    name="execute_command",
    description=(
        "Execute a shell command in the workspace, offline and isolated by default. "
        "The working directory persists across calls (cd works) and is returned as cwd "
        "on every result — do not cd into the directory you are already in. "
        "Long-running commands auto-background after timeout and return a command_id. "
        "Set run_in_background=true to background immediately and return a command_id. "
        "Follow up with get_command, wait_command, list_commands, or terminate_command. "
        "Large output includes offset, next_offset, and truncated so you can keep reading."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "network": {"type": "boolean", "default": False, "description": "Request network access for this invocation; requires separate approval and keeps file isolation"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds before auto-backgrounding (default: 20)",
                "default": 20,
            },
            "run_in_background": {
                "type": "boolean",
                "description": "If true, run immediately in the background and return command_id",
                "default": False,
            },
        },
        "required": ["command"],
    },
    call=lambda args, runtime: execute_command(**args, runtime=runtime),
    access_descriptor=describe_execute_command_access,
    is_concurrency_safe=is_execute_command_concurrency_safe,
    required_capabilities=frozenset({"execution", "cwd", "commands"}),
    timeout_owner="tool",
)

__all__ = ["execute_command", "execute_command_tool"]
