"""Tools that read or cancel one job run. The model still says schedule run."""

from __future__ import annotations

from typing import Any

from ....application.execution.identity import ExecutionWaitCancelled
from ....domain.model.tool import ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from .convert import TOOL_FAILURES, run_failure, run_view, scheduling


def list_schedule_runs(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        page = scheduling(runtime).list_runs(
            str(arguments["schedule_id"]) if arguments.get("schedule_id") else None,
            limit=int(arguments.get("limit", 100)),
            cursor=arguments.get("cursor"),
        )
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success({
        "count": len(page.records),
        "next_cursor": page.next_cursor,
        "runs": [run_view(record, summary=True) for record in page.records],
    })


def get_schedule_run(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    run_id = str(arguments["run_id"])
    try:
        return ToolResult.success(run_view(scheduling(runtime).get_run(run_id)))
    except TOOL_FAILURES as exc:
        return run_failure(exc, run_id=run_id)


def wait_schedule_run(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    run_id = str(arguments["run_id"])
    timeout = float(arguments.get("timeout", 30))
    try:
        run = scheduling(runtime).wait_run(
            run_id,
            timeout=timeout,
            cancellation_check=runtime.is_cancelled,
        )
    except (*TOOL_FAILURES, ExecutionWaitCancelled) as exc:
        return run_failure(exc, run_id=run_id)
    data = run_view(run)
    data["wait_completed"] = run.terminal
    data["wait_timed_out"] = not run.terminal
    return ToolResult.success(data)


def cancel_schedule_run(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    run_id = str(arguments["run_id"])
    reason = str(arguments.get("reason") or "schedule run cancelled by root Agent")[:1_000]
    try:
        result, schedule_status = scheduling(runtime).cancel_run(run_id, reason)
    except TOOL_FAILURES as exc:
        return run_failure(exc, run_id=run_id)
    data = run_view(result.run)
    data["already_terminal"] = not result.changed and result.run.terminal
    data["cooperative"] = result.cooperative
    data["changed"] = result.changed
    data["schedule_status"] = schedule_status
    data["schedule_changed"] = False
    data["message"] = (
        "Only this run was affected. Future triggers of the schedule were not changed."
    )
    return ToolResult.success(data)


def _describe_run_read(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("run_id") or arguments.get("schedule_id") or ""),
        reason="read a persisted schedule run",
    )


def _describe_run_cancel(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"persistent_write"}),
        subject=str(arguments.get("run_id") or ""),
        risk_flags=("persistent_automation",),
        reason="cancel one persisted schedule run without changing future triggers",
    )


_RUN_ID = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Identifier of one persisted schedule run. Not an agent_task_id, "
        "command_id, or schedule_id."
    ),
}


list_schedule_runs_tool = Tool(
    name="list_schedule_runs",
    description=(
        "List persisted schedule runs for this session. Optionally restrict the "
        "list to one schedule_id. Each run_id is a schedule run, not an agent "
        "delegation or a shell command. Status values such as queued, dispatched, "
        "and waiting_retry are returned as stored. Pass cursor from next_cursor "
        "to read older pages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "cursor": {"type": "string"},
        },
        "required": [],
        "additionalProperties": False,
    },
    call=list_schedule_runs,
    required_capabilities=frozenset({"scheduling"}),
    access_descriptor=_describe_run_read,
    is_concurrency_safe=lambda args: True,
    defer_to_model=True,
)


get_schedule_run_tool = Tool(
    name="get_schedule_run",
    description=(
        "Read one persisted schedule run by run_id. Returns the store's native "
        "status, including queued, dispatched, and waiting_retry. "
        "outcome=unconfirmed means the result cannot be confirmed and is not "
        "a successful completion. terminal says whether the run has finished waiting."
    ),
    parameters={
        "type": "object",
        "properties": {"run_id": _RUN_ID},
        "required": ["run_id"],
        "additionalProperties": False,
    },
    call=get_schedule_run,
    required_capabilities=frozenset({"scheduling"}),
    access_descriptor=_describe_run_read,
    is_concurrency_safe=lambda args: True,
    defer_to_model=True,
)


wait_schedule_run_tool = Tool(
    name="wait_schedule_run",
    description=(
        "Wait up to timeout seconds for one schedule run. A timeout sets "
        "wait_timed_out=true and does not cancel the run or mean it failed. "
        "The returned status is the store's native status. If this wait is "
        "itself cancelled, the run is left unchanged."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": _RUN_ID,
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": 300,
                "default": 30,
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    call=wait_schedule_run,
    required_capabilities=frozenset({"scheduling"}),
    access_descriptor=_describe_run_read,
    is_concurrency_safe=lambda args: True,
    timeout_owner="tool",
    defer_to_model=True,
)


cancel_schedule_run_tool = Tool(
    name="cancel_schedule_run",
    description=(
        "Cancel one persisted schedule run. Does not pause the schedule and does "
        "not change its future triggers. Queued, dispatched, and waiting_retry "
        "runs become cancelled; a running run is asked to stop and is not "
        "reported as already stopped until its status is terminal."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": _RUN_ID,
            "reason": {"type": "string", "maxLength": 1_000},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    call=cancel_schedule_run,
    required_capabilities=frozenset({"scheduling"}),
    access_descriptor=_describe_run_cancel,
    defer_to_model=True,
)
