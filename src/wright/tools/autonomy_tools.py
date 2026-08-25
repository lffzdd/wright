"""Root-only tools for durable schedules and their execution history."""

from __future__ import annotations

import time
from typing import Any

from ..autonomy import AutonomyStoreError, TriggerSpec
from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime


def _store(runtime: ToolRuntime):
    session = runtime.session_state
    store = getattr(session, "durable_task_store", None)
    if store is None:
        raise RuntimeError("durable task runtime is not configured")
    return store


def _notify(runtime: ToolRuntime) -> None:
    scheduler = getattr(runtime.session_state, "autonomy_scheduler", None)
    if scheduler is not None:
        scheduler.notify_changed()


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


def create_task(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        record = _store(runtime).create_automation(
            name=arguments["name"],
            prompt=arguments["prompt"],
            trigger=_trigger(arguments),
            recovery_policy=arguments.get("recovery_policy", "manual"),
            max_retries=int(arguments.get("max_retries", 0)),
            retry_delay_seconds=float(arguments.get("retry_delay_seconds", 30)),
        )
        _notify(runtime)
        return ToolResult.success(record.to_dict())
    except (RuntimeError, ValueError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))


def get_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        return ToolResult.success(
            _store(runtime).get_automation(str(arguments["schedule_id"])).to_dict()
        )
    except (RuntimeError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))


def list_schedules(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        records = _store(runtime).list_automations()
    except (RuntimeError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))
    status = arguments.get("status")
    if status is not None:
        records = [record for record in records if record.status == status]
    bounded = records[:100]
    summaries = []
    for record in bounded:
        row = record.to_dict()
        row["prompt"] = record.prompt[:500]
        row.pop("trigger_state", None)
        summaries.append(row)
    return ToolResult.success({
        "count": len(bounded),
        "truncated": len(records) > len(bounded),
        "schedules": summaries,
    })


def pause_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        record = _store(runtime).pause_automation(str(arguments["schedule_id"]))
        _notify(runtime)
        return ToolResult.success(record.to_dict())
    except (RuntimeError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))


def resume_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        record = _store(runtime).resume_automation(str(arguments["schedule_id"]))
        _notify(runtime)
        return ToolResult.success(record.to_dict())
    except (RuntimeError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))


def cancel_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    reason = str(arguments.get("reason") or "schedule cancelled by root Agent")
    try:
        record = _store(runtime).cancel_automation(
            str(arguments["schedule_id"]), reason
        )
        _notify(runtime)
        return ToolResult.success(record.to_dict())
    except (RuntimeError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))


def list_task_runs(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        records = _store(runtime).list_runs(
            str(arguments["schedule_id"])
            if arguments.get("schedule_id") else None
        )
    except (RuntimeError, AutonomyStoreError) as exc:
        return ToolResult.fail(str(exc))
    bounded = records[:100]
    summaries = []
    for record in bounded:
        row = record.to_dict()
        row["prompt"] = record.prompt[:500]
        row["result"] = record.result[:500]
        row["error"] = record.error[:500]
        row["trigger_payload"] = {
            "preview": str(record.trigger_payload)[:1_000]
        }
        summaries.append(row)
    return ToolResult.success({
        "count": len(bounded),
        "truncated": len(records) > len(bounded),
        "runs": summaries,
    })


def _persistent_mutation_permission(
    arguments: dict[str, Any], runtime: ToolRuntime
) -> PermissionCheckResult:
    return PermissionCheckResult(
        "ask",
        "This changes a durable automation that can wake the Agent later",
        ("persistent_automation",),
        source="autonomy_tool",
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


create_task_tool = Tool(
    name="create_task",
    description=(
        "Create a durable autonomous task that runs in an isolated session. "
        "It cannot see the current conversation, user goal, plan, or transcript. "
        "Use this for work that must survive process restart or must not share "
        "the live dialogue. Trigger types: once (delay_seconds or epoch run_at), "
        "interval, file_change inside workspace, web_change for a public HTTP(S) "
        "page, or named external event. Recovery defaults to manual/no blind replay. "
        "For recurring checks that should see the current conversation and die "
        "with this session, use the loop tool or /loop instead."
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
        },
        "required": ["name", "prompt", "trigger"],
        "additionalProperties": False,
    },
    call=create_task,
    check_permission=_persistent_mutation_permission,
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
    is_concurrency_safe=lambda args: True,
)


list_schedules_tool = Tool(
    name="list_schedules",
    description="List durable schedules, optionally filtered by lifecycle status.",
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "paused", "completed", "cancelled"],
            }
        },
        "required": [],
        "additionalProperties": False,
    },
    call=list_schedules,
    is_concurrency_safe=lambda args: True,
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
        check_permission=_persistent_mutation_permission,
    )


pause_schedule_tool = _schedule_mutation_tool(
    "pause_schedule", "Pause future triggers without cancelling an active run.", pause_schedule
)
resume_schedule_tool = _schedule_mutation_tool(
    "resume_schedule", "Resume a paused durable schedule.", resume_schedule
)
cancel_schedule_tool = _schedule_mutation_tool(
    "cancel_schedule",
    "Permanently cancel future triggers and request cancellation of active runs.",
    cancel_schedule,
)


list_task_runs_tool = Tool(
    name="list_task_runs",
    description=(
        "List durable execution history. Optionally restrict it to one schedule_id; "
        "individual run ids also work with get_task/wait_task/cancel_task."
    ),
    parameters={
        "type": "object",
        "properties": {"schedule_id": {"type": "string", "minLength": 1}},
        "required": [],
        "additionalProperties": False,
    },
    call=list_task_runs,
    is_concurrency_safe=lambda args: True,
)


autonomy_tools = [
    create_task_tool,
    get_schedule_tool,
    list_schedules_tool,
    pause_schedule_tool,
    resume_schedule_tool,
    cancel_schedule_tool,
    list_task_runs_tool,
]
