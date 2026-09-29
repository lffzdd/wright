"""Map job records to the public schedule tool shape.

Tool names, ``schedule_id``, ``run_id``, and ``automation_name`` stay as the
model already sees them. Scheduling code itself uses job definitions and runs.
"""

from __future__ import annotations

import time
from typing import Any

from ....application.execution.identity import (
    ExecutionKindMismatch,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)
from ....application.scheduling.contracts import SchedulingError
from ....domain.model.scheduling import JobDefinition, JobRun, TriggerSpec
from ..runtime import ToolRuntime


def scheduling(runtime: ToolRuntime):
    operations = runtime.capabilities.scheduling if runtime.capabilities else None
    if operations is None:
        raise RuntimeError("schedule tools require scheduling operations")
    return operations


def schedule_view(record: JobDefinition, *, summary: bool = False) -> dict[str, Any]:
    row = record.to_dict()
    row["schedule_id"] = row.pop("id")
    if summary:
        row["prompt"] = record.prompt[:500]
        row.pop("trigger_state", None)
    return row


def run_view(record: JobRun, *, summary: bool = False) -> dict[str, Any]:
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
        "schedule_id": record.job_id,
        "automation_name": record.job_name,
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


def run_failure(exc: Exception, *, run_id: str):
    from ....domain.model.tool import ToolResult

    if isinstance(exc, ExecutionKindMismatch):
        return ToolResult.fail(str(exc))
    if isinstance(exc, ExecutionNotFound):
        return ToolResult.fail(f"Unknown run_id: {run_id}")
    if isinstance(exc, ExecutionWaitCancelled):
        return ToolResult.fail(
            "This wait was cancelled. The schedule run was not cancelled."
        )
    return ToolResult.fail(str(exc))


TOOL_FAILURES = (
    RuntimeError,
    ValueError,
    SchedulingError,
    ExecutionKindMismatch,
    ExecutionNotFound,
)


def parse_trigger(arguments: dict[str, Any]) -> TriggerSpec:
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


TRIGGER_SCHEMA = {
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
