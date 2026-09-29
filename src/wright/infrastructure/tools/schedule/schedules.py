"""Tools that create and change a job definition. The model still says schedule."""

from __future__ import annotations

from typing import Any

from ....domain.model.tool import ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from .convert import (
    TOOL_FAILURES,
    TRIGGER_SCHEMA,
    parse_trigger,
    schedule_view,
    scheduling,
)


def create_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        record = scheduling(runtime).create_job(
            name=arguments["name"],
            prompt=arguments["prompt"],
            trigger=parse_trigger(arguments),
            recovery_policy=arguments.get("recovery_policy", "manual"),
            max_retries=int(arguments.get("max_retries", 0)),
            retry_delay_seconds=float(arguments.get("retry_delay_seconds", 30)),
            run_config=arguments.get("run_config"),
        )
        return ToolResult.success(schedule_view(record))
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))


def get_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        return ToolResult.success(
            schedule_view(scheduling(runtime).get_job(str(arguments["schedule_id"])))
        )
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))


def list_schedules(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        page = scheduling(runtime).list_jobs(
            status=arguments.get("status"),
            limit=int(arguments.get("limit", 100)),
            cursor=arguments.get("cursor"),
        )
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success({
        "count": len(page.records),
        "next_cursor": page.next_cursor,
        "schedules": [schedule_view(record, summary=True) for record in page.records],
    })


def pause_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        change = scheduling(runtime).pause_job(str(arguments["schedule_id"]))
        data = schedule_view(change.job)
        data["changed"] = change.changed
        return ToolResult.success(data)
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))


def resume_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        change = scheduling(runtime).resume_job(str(arguments["schedule_id"]))
        data = schedule_view(change.job)
        data["changed"] = change.changed
        return ToolResult.success(data)
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))


def cancel_schedule(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    reason = str(arguments.get("reason") or "schedule cancelled by root Agent")
    try:
        change = scheduling(runtime).cancel_job(str(arguments["schedule_id"]), reason)
        data = schedule_view(change.job)
        data["changed"] = change.changed
        return ToolResult.success(data)
    except TOOL_FAILURES as exc:
        return ToolResult.fail(str(exc))


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
            "trigger": TRIGGER_SCHEMA,
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
    required_capabilities=frozenset({"scheduling"}),
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
    required_capabilities=frozenset({"scheduling"}),
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
    required_capabilities=frozenset({"scheduling"}),
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
        required_capabilities=frozenset({"scheduling"}),
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
