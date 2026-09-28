"""Root-only tools for durable schedules and their execution history."""

from __future__ import annotations

import time
from typing import Any

from ...application.execution.identity import (
    ExecutionKindMismatch,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)
from ...domain.model.automation import DurableRunRecord, TriggerSpec
from ...domain.model.tool import ToolAccess, ToolResult
from ...infrastructure.persistence.autonomy_store import AutonomyStoreError
from .base import Tool
from .runtime import ToolRuntime


def _autonomy(runtime: ToolRuntime):
    autonomy = runtime.capabilities.autonomy if runtime.capabilities else None
    if autonomy is None:
        raise RuntimeError("schedule tools require autonomy operations")
    return autonomy


def _schedule_view(record, *, summary: bool = False) -> dict[str, Any]:
    row = record.to_dict()
    row["schedule_id"] = row.pop("id")
    if summary:
        row["prompt"] = record.prompt[:500]
        row.pop("trigger_state", None)
    return row


def _run_view(record: DurableRunRecord, *, summary: bool = False) -> dict[str, Any]:
    """Expose the store's native run status. Do not flatten it to pending."""
    if record.status == "unknown":
        outcome = "unconfirmed"
    elif record.terminal:
        outcome = record.status
    else:
        outcome = "waiting"
    text = 500 if summary else 8_000
    view: dict[str, Any] = {
        "run_id": record.id,
        "schedule_id": record.automation_id,
        "automation_name": record.automation_name,
        "status": record.status,
        "terminal": record.terminal,
        "outcome": outcome,
        "prompt": record.prompt[:text],
        "trigger_type": record.trigger_type,
        "result": record.result[:text],
        "error": record.error[:text],
        "cancel_requested": record.cancel_requested,
        "cancel_reason": record.cancel_reason[:text],
        "attempt": record.attempt,
        "max_retries": record.max_retries,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
    }
    if summary:
        view["trigger_payload"] = {"preview": str(record.trigger_payload)[:1_000]}
    else:
        view["trigger_payload"] = dict(record.trigger_payload)
    if record.status == "unknown":
        view["note"] = (
            "Status cannot be confirmed. This is not a successful completion."
        )
    return view


def _run_failure(exc: Exception, *, run_id: str) -> ToolResult:
    if isinstance(exc, ExecutionKindMismatch):
        return ToolResult.fail(str(exc))
    if isinstance(exc, ExecutionNotFound):
        return ToolResult.fail(f"Unknown run_id: {run_id}")
    if isinstance(exc, ExecutionWaitCancelled):
        return ToolResult.fail(
            "This wait was cancelled. The schedule run was not cancelled."
        )
    return ToolResult.fail(str(exc))


def _trigger(arguments: dict[str, Any]) -> TriggerSpec:
    value = arguments["trigger"]
    trigger_type = value["type"]
    now = time.time()
    if trigger_type == "once":
        if "run_at" in value:
            run_at = float(value["run_at"])
        else:
            run_at = now + float(value.get("delay_seconds", 0))
        return TriggerSpec(type="once", run_at=run_at)
    if trigger_type == "interval":
        if "every_seconds" not in value:
            raise ValueError("interval trigger requires every_seconds")
        return TriggerSpec(
            type="interval",
            every_seconds=float(value["every_seconds"]),
            start_at=now + float(value.get("start_in_seconds", value["every_seconds"])),
        )
    if trigger_type == "file_change":
        if "path" not in value:
            raise ValueError("file_change trigger requires path")
        return TriggerSpec(type="file_change", path=str(value["path"]))
    if trigger_type == "web_change":
        if "url" not in value or "every_seconds" not in value:
            raise ValueError("web_change trigger requires url and every_seconds")
        return TriggerSpec(
            type="web_change",
            url=str(value["url"]),
            every_seconds=float(value["every_seconds"]),
        )
    if trigger_type == "event":
        if "event_name" not in value:
            raise ValueError("event trigger requires event_name")
        return TriggerSpec(type="event", event_name=str(value["event_name"]))
    raise ValueError(f"unsupported trigger type: {trigger_type}")


def create_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        record = _autonomy(runtime).create_schedule(
            name=arguments["name"],
            prompt=arguments["prompt"],
            trigger=_trigger(arguments),
            recovery_policy=arguments.get("recovery_policy", "manual"),
            max_retries=int(arguments.get("max_retries", 0)),
            retry_delay_seconds=float(arguments.get("retry_delay_seconds", 30)),
            run_config=arguments.get("run_config"),
        )
        return ToolResult.success(_schedule_view(record))
    except (RuntimeError, ValueError, AutonomyStoreError, ExecutionKindMismatch, ExecutionNotFound) as exc:
        return ToolResult.fail(str(exc))


def get_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        return ToolResult.success(
            _schedule_view(_autonomy(runtime).get_schedule(str(arguments["schedule_id"])))
        )
    except (RuntimeError, AutonomyStoreError, ExecutionKindMismatch, ExecutionNotFound) as exc:
        return ToolResult.fail(str(exc))


def list_schedules(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        page = _autonomy(runtime).list_schedules(
            status=arguments.get("status"),
            limit=int(arguments.get("limit", 100)),
            cursor=arguments.get("cursor"),
        )
    except (RuntimeError, AutonomyStoreError, ValueError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success({
        "count": len(page.records),
        "next_cursor": page.next_cursor,
        "schedules": [_schedule_view(record, summary=True) for record in page.records],
    })


def pause_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        change = _autonomy(runtime).pause_schedule(str(arguments["schedule_id"]))
        data = _schedule_view(change.automation)
        data["changed"] = change.changed
        return ToolResult.success(data)
    except (RuntimeError, AutonomyStoreError, ExecutionKindMismatch, ExecutionNotFound) as exc:
        return ToolResult.fail(str(exc))


def resume_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        change = _autonomy(runtime).resume_schedule(str(arguments["schedule_id"]))
        data = _schedule_view(change.automation)
        data["changed"] = change.changed
        return ToolResult.success(data)
    except (RuntimeError, AutonomyStoreError, ExecutionKindMismatch, ExecutionNotFound) as exc:
        return ToolResult.fail(str(exc))


def cancel_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    reason = str(arguments.get("reason") or "schedule cancelled by root Agent")
    try:
        change = _autonomy(runtime).cancel_schedule(str(arguments["schedule_id"]), reason)
        data = _schedule_view(change.automation)
        data["changed"] = change.changed
        return ToolResult.success(data)
    except (RuntimeError, AutonomyStoreError, ExecutionKindMismatch, ExecutionNotFound) as exc:
        return ToolResult.fail(str(exc))


def list_schedule_runs(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        page = _autonomy(runtime).list_runs(
            str(arguments["schedule_id"]) if arguments.get("schedule_id") else None,
            limit=int(arguments.get("limit", 100)),
            cursor=arguments.get("cursor"),
        )
    except (RuntimeError, AutonomyStoreError, ExecutionKindMismatch, ExecutionNotFound, ValueError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success({
        "count": len(page.records),
        "next_cursor": page.next_cursor,
        "runs": [_run_view(record, summary=True) for record in page.records],
    })


def get_schedule_run(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    run_id = str(arguments["run_id"])
    try:
        return ToolResult.success(_run_view(_autonomy(runtime).get_run(run_id)))
    except (
        RuntimeError, ExecutionKindMismatch, ExecutionNotFound, AutonomyStoreError
    ) as exc:
        return _run_failure(exc, run_id=run_id)


def wait_schedule_run(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    run_id = str(arguments["run_id"])
    timeout = float(arguments.get("timeout", 30))
    try:
        run = _autonomy(runtime).wait_run(
            run_id,
            timeout=timeout,
            cancellation_check=runtime.is_cancelled,
        )
    except (
        RuntimeError,
        ExecutionKindMismatch,
        ExecutionNotFound,
        ExecutionWaitCancelled,
        AutonomyStoreError,
        ValueError,
    ) as exc:
        return _run_failure(exc, run_id=run_id)
    data = _run_view(run)
    data["wait_completed"] = run.terminal
    data["wait_timed_out"] = not run.terminal
    return ToolResult.success(data)


def cancel_schedule_run(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    run_id = str(arguments["run_id"])
    reason = str(arguments.get("reason") or "schedule run cancelled by root Agent")[:1_000]
    try:
        result, schedule_status = _autonomy(runtime).cancel_run(run_id, reason)
    except (
        RuntimeError, ExecutionKindMismatch, ExecutionNotFound, AutonomyStoreError
    ) as exc:
        return _run_failure(exc, run_id=run_id)
    data = _run_view(result.run)
    data["already_terminal"] = not result.changed and result.run.terminal
    data["cooperative"] = result.cooperative
    data["changed"] = result.changed
    data["schedule_status"] = schedule_status
    data["schedule_changed"] = False
    data["message"] = (
        "Only this run was affected. Future triggers of the schedule were not changed."
    )
    return ToolResult.success(data)


def _describe_persistent_mutation(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"persistent_write"}),
        subject=str(arguments.get("schedule_id") or arguments.get("name") or ""),
        risk_flags=("persistent_automation",),
        reason="change a durable automation that can wake the Agent later",
    )


def _describe_schedule_read(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("schedule_id") or ""),
        reason="read durable automation state",
    )


_TRIGGER_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "once", "interval", "file_change", "web_change", "event"
            ],
        },
        "delay_seconds": {"type": "number", "minimum": 0},
        "run_at": {"type": "number", "minimum": 0},
        "every_seconds": {"type": "number", "minimum": 1},
        "start_in_seconds": {"type": "number", "minimum": 0},
        "path": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "url": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "event_name": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["type"],
    "additionalProperties": False,
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "interval"}}},
            "then": {"required": ["every_seconds"]},
        },
        {
            "if": {"properties": {"type": {"const": "file_change"}}},
            "then": {"required": ["path"]},
        },
        {
            "if": {"properties": {"type": {"const": "web_change"}}},
            "then": {"required": ["url", "every_seconds"]},
        },
        {
            "if": {"properties": {"type": {"const": "event"}}},
            "then": {"required": ["event_name"]},
        },
    ],
}


create_schedule_tool = Tool(
    name="create_schedule",
    description=(
        "Create a durable schedule that runs in an isolated session. "
        "The returned schedule_id identifies the rule, not one execution. "
        "It cannot see the current conversation, user goal, plan, or transcript. "
        "Use this for work that must survive process restart or must not share "
        "the live dialogue. Trigger types: once (delay_seconds or epoch run_at), "
        "interval, file_change inside workspace, web_change for a public HTTP(S) "
        "page, or named external event. Recovery defaults to manual/no blind replay. "
        "Inspect a concrete execution with list_schedule_runs, get_schedule_run, "
        "wait_schedule_run, or cancel_schedule_run. run_id there is a persisted "
        "schedule run, not an agent delegation or a shell command. "
        "For recurring checks that should see the current conversation and die "
        "with this session, use manage_loop or /loop instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 4_000},
            "trigger": _TRIGGER_SCHEMA,
            "recovery_policy": {
                "type": "string",
                "enum": ["manual", "retry"],
                "default": "manual",
            },
            "max_retries": {
                "type": "integer", "minimum": 0, "maximum": 20, "default": 0,
            },
            "retry_delay_seconds": {
                "type": "number", "minimum": 0, "maximum": 86_400, "default": 30,
            },
            "run_config": {
                "type": "object",
                "description": "Snapshot for this durable run only; it cannot grant extra permissions.",
                "properties": {
                    "profile": {"type": "string", "enum": ["durable"]},
                    "model": {"type": "string", "minLength": 1, "maxLength": 200},
                    "transport": {"type": "string", "enum": ["auto", "chat", "responses"]},
                    "environment": {"type": "string", "enum": ["local"]},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "additionalProperties": False,
            },
        },
        "required": ["name", "prompt", "trigger"],
        "additionalProperties": False,
    },
    call=create_schedule,
    required_capabilities=frozenset({"autonomy"}),
    access_descriptor=_describe_persistent_mutation,
    defer_to_model=True,
)


get_schedule_tool = Tool(
    name="get_schedule",
    description="Read one durable schedule definition and its next-run metadata.",
    parameters={
        "type": "object",
        "properties": {"schedule_id": {"type": "string", "minLength": 1}},
        "required": ["schedule_id"],
        "additionalProperties": False,
    },
    call=get_schedule,
    required_capabilities=frozenset({"autonomy"}),
    access_descriptor=_describe_schedule_read,
    is_concurrency_safe=lambda args: True,
    defer_to_model=True,
)


list_schedules_tool = Tool(
    name="list_schedules",
    description=(
        "List durable schedules, optionally filtered by lifecycle status. "
        "Pass cursor from next_cursor to read older pages."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "paused", "completed", "cancelled"],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "cursor": {"type": "string"},
        },
        "required": [],
        "additionalProperties": False,
    },
    call=list_schedules,
    required_capabilities=frozenset({"autonomy"}),
    access_descriptor=_describe_schedule_read,
    is_concurrency_safe=lambda args: True,
    defer_to_model=True,
)


def _schedule_mutation_tool(name: str, description: str, call) -> Tool:
    properties: dict[str, Any] = {
        "schedule_id": {"type": "string", "minLength": 1}
    }
    if name == "cancel_schedule":
        properties["reason"] = {"type": "string", "maxLength": 1_000}
    return Tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": ["schedule_id"],
            "additionalProperties": False,
        },
        call=call,
        required_capabilities=frozenset({"autonomy"}),
        access_descriptor=_describe_persistent_mutation,
        defer_to_model=True,
    )


pause_schedule_tool = _schedule_mutation_tool(
    "pause_schedule",
    "Pause future triggers of one schedule. Does not cancel a run that is already executing or queued.",
    pause_schedule,
)
resume_schedule_tool = _schedule_mutation_tool(
    "resume_schedule", "Resume a paused durable schedule.", resume_schedule
)
cancel_schedule_tool = _schedule_mutation_tool(
    "cancel_schedule",
    "Stop future triggers and request cancellation of this schedule's active runs. "
    "This changes the rule. To cancel only one run, use cancel_schedule_run.",
    cancel_schedule,
)


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
    required_capabilities=frozenset({"autonomy"}),
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
    required_capabilities=frozenset({"autonomy"}),
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
    required_capabilities=frozenset({"autonomy"}),
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
    required_capabilities=frozenset({"autonomy"}),
    access_descriptor=_describe_run_cancel,
    defer_to_model=True,
)


autonomy_tools = [
    create_schedule_tool,
    get_schedule_tool,
    list_schedules_tool,
    pause_schedule_tool,
    resume_schedule_tool,
    cancel_schedule_tool,
    list_schedule_runs_tool,
    get_schedule_run_tool,
    wait_schedule_run_tool,
    cancel_schedule_run_tool,
]
