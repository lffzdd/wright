"""Root-only tools for one delegated agent execution.

``agent_task_id`` identifies that delegation. It is not a reusable Agent
instance. State stays on the Agent control plane.
"""

from __future__ import annotations

from typing import Any

from ...application.tasks.service import TaskService
from ...domain.model.coordination import TERMINAL_AGENT_TASK_STATUSES, AgentTaskRecord
from ...domain.model.tasks import TaskKindMismatch, TaskNotFoundError, TaskWaitCancelled
from ...domain.model.tool import ToolAccess, ToolResult
from .base import Tool
from .runtime import ToolRuntime


def agent_execution_view(record: AgentTaskRecord) -> dict[str, Any]:
    """Model view of one delegation. Fields come from the control-plane record."""
    terminal = record.status in TERMINAL_AGENT_TASK_STATUSES
    view: dict[str, Any] = {
        "agent_task_id": record.id,
        "status": record.status,
        "terminal": terminal,
        "parent_agent_task_id": record.parent_id,
        "children": list(record.children),
        "depth": record.depth,
        "description": record.task,
        "result": record.result,
        "error": record.error,
        "usage": {
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
        },
        "steps_used": record.steps_used,
        "step_budget": record.step_budget,
        "cancel_requested": record.cancel_requested,
        "cancel_reason": record.cancel_reason,
    }
    if record.cancel_requested and not terminal:
        view["message"] = (
            "Cancellation was requested. The delegation has not stopped yet."
        )
    return view


def public_agent_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rename control-plane ids at the model boundary."""
    projected: list[dict[str, Any]] = []
    for node in nodes:
        children = node.get("children") or []
        row = {
            key: value
            for key, value in node.items()
            if key not in {"id", "parent_id", "children"}
        }
        row["agent_task_id"] = node.get("id", "")
        row["parent_agent_task_id"] = node.get("parent_id")
        row["children"] = public_agent_tree(list(children))
        projected.append(row)
    return projected


def _service(runtime: ToolRuntime) -> TaskService:
    service = runtime.capabilities.tasks if runtime.capabilities else None
    if service is None:
        raise RuntimeError("agent tool requires task classification")
    return service


def _observe(exc: Exception, *, agent_task_id: str) -> ToolResult:
    if isinstance(exc, TaskKindMismatch):
        return ToolResult.fail(str(exc))
    if isinstance(exc, TaskNotFoundError):
        return ToolResult.fail(f"Unknown agent_task_id: {agent_task_id}")
    if isinstance(exc, TaskWaitCancelled):
        return ToolResult.fail(
            "This wait was cancelled. The agent delegation was not cancelled."
        )
    return ToolResult.fail(str(exc))


def get_agent(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    agent_task_id = str(arguments["agent_task_id"])
    try:
        record = _service(runtime).agent_record(agent_task_id)
    except (RuntimeError, TaskKindMismatch, TaskNotFoundError) as exc:
        return _observe(exc, agent_task_id=agent_task_id)
    return ToolResult.success(agent_execution_view(record))


def wait_agent(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    agent_task_id = str(arguments["agent_task_id"])
    timeout = float(arguments.get("timeout", 30))
    try:
        service = _service(runtime)
        service.wait_kind(
            agent_task_id,
            "agent",
            timeout=timeout,
            cancellation_check=runtime.is_cancelled,
        )
        record = service.agent_record(agent_task_id)
    except (RuntimeError, TaskKindMismatch, TaskNotFoundError, TaskWaitCancelled, ValueError) as exc:
        return _observe(exc, agent_task_id=agent_task_id)
    data = agent_execution_view(record)
    data["wait_completed"] = data["terminal"]
    data["wait_timed_out"] = not data["terminal"]
    return ToolResult.success(data)


def cancel_agent(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    agent_task_id = str(arguments["agent_task_id"])
    reason = str(arguments.get("reason") or "Root Agent requested cancellation")[:1_000]
    try:
        service = _service(runtime)
        before = service.agent_record(agent_task_id)
        service.cancel_kind(agent_task_id, "agent", reason=reason)
        record = service.agent_record(agent_task_id)
    except (RuntimeError, TaskKindMismatch, TaskNotFoundError) as exc:
        return _observe(exc, agent_task_id=agent_task_id)
    data = agent_execution_view(record)
    data["already_terminal"] = before.status in TERMINAL_AGENT_TASK_STATUSES
    return ToolResult.success(data)


def _describe_read(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("agent_task_id") or ""),
        reason="observe a delegated agent execution",
    )


def _describe_cancel(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"execution_control"}),
        subject=str(arguments.get("agent_task_id") or ""),
        risk_flags=("controls_live_execution",),
        reason="request cooperative cancellation of a delegated agent execution",
    )


_ID = {
    "type": "object",
    "properties": {
        "agent_task_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Identifier of one delegated execution returned by spawn_agent. "
                "It is not a reusable Agent instance."
            ),
        }
    },
    "required": ["agent_task_id"],
    "additionalProperties": False,
}


get_agent_tool = Tool(
    name="get_agent",
    description=(
        "Read one delegated agent execution by agent_task_id. Returns its status, "
        "result, error, parent and child delegations, usage, and cancellation state. "
        "cancel_requested means cancellation was requested; a non-terminal status "
        "means the delegation has not stopped. Use get_agent_tree to list delegations "
        "for the current user turn."
    ),
    parameters=_ID,
    call=get_agent,
    access_descriptor=_describe_read,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: True,
)


wait_agent_tool = Tool(
    name="wait_agent",
    description=(
        "Wait up to timeout seconds for one delegated agent execution. A timeout is "
        "an observation with wait_timed_out=true; it does not cancel the delegation "
        "and does not mean the execution failed. If this wait is itself cancelled, "
        "the delegation is left running."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_task_id": _ID["properties"]["agent_task_id"],
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": 300,
                "default": 30,
            },
        },
        "required": ["agent_task_id"],
        "additionalProperties": False,
    },
    call=wait_agent,
    access_descriptor=_describe_read,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: True,
    timeout_owner="tool",
)


cancel_agent_tool = Tool(
    name="cancel_agent",
    description=(
        "Request cooperative cancellation of one delegated agent execution and its "
        "descendants. The call reports the current status after the request. "
        "cancel_requested does not mean the delegation has already stopped. "
        "Cancelling an already terminal delegation does not change it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent_task_id": _ID["properties"]["agent_task_id"],
            "reason": {"type": "string", "maxLength": 1_000},
        },
        "required": ["agent_task_id"],
        "additionalProperties": False,
    },
    call=cancel_agent,
    access_descriptor=_describe_cancel,
    required_capabilities=frozenset({"tasks"}),
    is_concurrency_safe=lambda args: False,
)


agent_tools = [get_agent_tool, wait_agent_tool, cancel_agent_tool]
