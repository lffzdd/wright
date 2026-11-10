"""Expose the session PlanManager as model-callable tools."""

from __future__ import annotations

from ...domain.feedback import system_text
from ...domain.model.planning.plan import PlanError
from ...domain.model.tool import ToolAccess, ToolResult
from ...domain.model.tool_definition import Tool
from ...domain.model.tool_runtime import ToolRuntime
from .system_result import fail_text


def _plan_failure(exc: Exception) -> ToolResult:
    if isinstance(exc, PlanError) and exc.code:
        return fail_text(system_text(exc.code, str(exc), **exc.params))
    return ToolResult.fail(str(exc))


def _describe_plan_update(arguments: dict) -> ToolAccess:
    return ToolAccess(
        frozenset({"plan_update"}),
        subject=str(arguments.get("objective") or ""),
        reason="update the current session plan",
    )


def _manager(runtime: ToolRuntime | None):
    manager = runtime.capabilities.plan_manager if runtime and runtime.capabilities else None
    if manager is None:
        raise RuntimeError("plan tool requires a Session with PlanManager")
    return manager


def update_plan(
    steps: list[dict],
    objective: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(_manager(runtime).apply_steps(steps, objective=objective))
    except Exception as e:
        return _plan_failure(e)


update_plan_tool = Tool(
    name="update_plan",
    description=(
        "Write the task plan as the whole list of steps with their status. Use it when "
        "the work has several steps, multiple tools, or needs progress tracking; skip "
        "it for a one-step question. Send the full list every time: mark a step "
        "in_progress when you start it and completed as soon as it is done. At most "
        "one step is in_progress, and steps finish in order. Change the route by "
        "editing the list: steps you drop are kept as skipped history, finished "
        "steps cannot be reopened. objective is required to start a new plan."
    ),
    parameters={
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "The goal of the plan; required when starting one",
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "description": "All steps in execution order",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short step title"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "blocked", "skipped"],
                            "default": "pending",
                        },
                        "note": {"type": "string", "description": "Optional progress, result, or blocker"},
                    },
                    "required": ["title", "status"],
                },
            },
        },
        "required": ["steps"],
    },
    call=lambda args, runtime: update_plan(**args, runtime=runtime),
    access_descriptor=_describe_plan_update,
    required_capabilities=frozenset({"plan"}),
)


plan_tools: list[Tool] = [update_plan_tool]
