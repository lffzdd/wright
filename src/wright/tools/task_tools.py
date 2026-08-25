"""Model-facing tools for the unified runtime task facade."""

from __future__ import annotations

from typing import Any

from ..tasks import TaskNotFoundError, TaskService, TaskWaitCancelled
from .base import Tool, ToolResult, ToolRuntime


def _service(runtime: ToolRuntime) -> TaskService:
    if runtime.session_state is None:
        raise RuntimeError("task tool requires a SessionState runtime")
    return TaskService.for_session(runtime.session_state)


def get_task(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        task = _service(runtime).get(str(arguments["task_id"]))
    except (RuntimeError, TaskNotFoundError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success(task.to_dict())


def wait_task(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    timeout = float(arguments.get("timeout", 30))
    try:
        task = _service(runtime).wait(
            str(arguments["task_id"]),
            timeout=timeout,
            cancellation_check=runtime.is_cancelled,
        )
    except (RuntimeError, TaskNotFoundError, TaskWaitCancelled, ValueError) as exc:
        return ToolResult.fail(str(exc))
    data = task.to_dict()
    data["wait_completed"] = task.terminal
    data["wait_timed_out"] = not task.terminal
    return ToolResult.success(data)


def cancel_task(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    reason = str(arguments.get("reason") or "主 Agent 请求取消")[:1_000]
    try:
        service = _service(runtime)
        before = service.get(str(arguments["task_id"]))
        task = service.cancel(str(arguments["task_id"]), reason=reason)
    except (RuntimeError, TaskNotFoundError) as exc:
        return ToolResult.fail(str(exc))
    data = task.to_dict()
    data["already_terminal"] = before.terminal
    return ToolResult.success(data)


def list_tasks(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        session = runtime.session_state
        if session is None:
            raise RuntimeError("task tool requires a SessionState runtime")
        include_all = bool(arguments.get("include_all_turns", False))
        tasks = _service(runtime).list(
            kind=arguments.get("kind"),
            status=arguments.get("status"),
            root_turn_id=None if include_all else session.agent_root_turn_id,
        )
    except (RuntimeError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    bounded = tasks[:100]
    summaries = []
    for task in bounded:
        row = task.to_dict(include_details=False)
        row["description"] = task.description[:500]
        row["result"] = task.result[:500]
        row["output"] = task.output[-500:]
        row["error"] = task.error[:500]
        summaries.append(row)
    return ToolResult.success({
        "count": len(bounded),
        "truncated": len(tasks) > len(bounded),
        "tasks": summaries,
    })


get_task_tool = Tool(
    name="get_task",
    description=(
        "Read any Agent, shell, or durable task through one common task_id API. "
        "Returns its kind, lifecycle status, result/output, error, and cancellation state."
    ),
    parameters={
        "type": "object",
        "properties": {"task_id": {"type": "string", "minLength": 1}},
        "required": ["task_id"],
        "additionalProperties": False,
    },
    call=get_task,
    is_concurrency_safe=lambda args: True,
)


wait_task_tool = Tool(
    name="wait_task",
    description=(
        "Wait up to timeout seconds for any Agent, shell, or durable task. A timeout is a "
        "successful observation with wait_timed_out=true; it does not cancel the task."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": 300,
                "default": 30,
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
    call=wait_task,
    is_concurrency_safe=lambda args: True,
    timeout_owner="tool",
)


cancel_task_tool = Tool(
    name="cancel_task",
    description=(
        "Cancel any Agent, shell, or durable task. Agent/durable running cancellation "
        "is cooperative; shell cancellation terminates the process tree."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "maxLength": 1_000},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
    call=cancel_task,
    # Shell cancellation mutates a live process tree; serialize it with other
    # exclusive tools in the same model turn.
    is_concurrency_safe=lambda args: False,
)


list_tasks_tool = Tool(
    name="list_tasks",
    description=(
        "List Agent, shell, and durable tasks for the current user turn. Filter by kind or "
        "status; include_all_turns is intended for history/debugging."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["agent", "shell", "durable"],
            },
            "status": {
                "type": "string",
                "enum": [
                    "pending", "running", "completed", "failed",
                    "cancelled", "timed_out", "unknown",
                ],
            },
            "include_all_turns": {"type": "boolean", "default": False},
        },
        "required": [],
        "additionalProperties": False,
    },
    call=list_tasks,
    is_concurrency_safe=lambda args: True,
)


task_tools = [get_task_tool, wait_task_tool, cancel_task_tool, list_tasks_tool]
