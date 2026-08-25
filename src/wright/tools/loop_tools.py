"""Root-only in-session loop tool. Not persisted; not given to sub-agents."""

from __future__ import annotations

from typing import Any

from ..looping import LoopError, SessionLoopRegistry
from .base import Tool, ToolResult, ToolRuntime


def _registry(runtime: ToolRuntime) -> SessionLoopRegistry:
    session = runtime.session_state
    registry = getattr(session, "loop_registry", None)
    if registry is None:
        raise RuntimeError("in-session loop runtime is not configured")
    return registry


def loop_tool_call(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    try:
        registry = _registry(runtime)
        action = str(arguments.get("action") or "create")
        if action == "create":
            if "interval_seconds" not in arguments or "prompt" not in arguments:
                raise LoopError("create requires interval_seconds and prompt")
            record = registry.create(
                prompt=str(arguments["prompt"]),
                interval_seconds=float(arguments["interval_seconds"]),
                name=str(arguments.get("name") or ""),
            )
            return ToolResult.success(record.to_dict())
        if action == "list":
            records = registry.list_loops()
            return ToolResult.success({
                "count": len(records),
                "loops": [record.to_dict() for record in records],
            })
        if action == "stop":
            loop_id = str(arguments.get("loop_id") or "").strip()
            if not loop_id:
                raise LoopError("stop requires loop_id")
            return ToolResult.success(registry.stop(loop_id).to_dict())
        raise LoopError("action must be create, list, or stop")
    except (RuntimeError, TypeError, ValueError) as exc:
        return ToolResult.fail(str(exc))


loop_tool = Tool(
    name="loop",
    description=(
        "Create or manage an in-session recurring prompt (like /loop). "
        "Each tick runs in THIS conversation via a runtime event: it sees the "
        "current user goal, plan, and transcript. Loops are memory-only — they "
        "vanish when the session ends and do not survive restart. "
        "Do not use this for work that must outlive the session or run without "
        "the current conversation; use create_task for isolated durable runs. "
        "Interval must be at least 5 seconds; at most 20 loops per session."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "stop"],
                "description": "create a loop, list active loops, or stop one",
            },
            "interval_seconds": {
                "type": "number",
                "minimum": 5,
                "description": "Fixed delay between ticks; minimum 5 seconds",
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4_000,
                "description": "What to re-run in the current conversation",
            },
            "name": {
                "type": "string",
                "maxLength": 200,
                "description": "Optional short label; defaults to a prompt preview",
            },
            "loop_id": {
                "type": "string",
                "minLength": 1,
                "description": "Required when action is stop",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    call=loop_tool_call,
)
