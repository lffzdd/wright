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


def _acknowledge(plan: dict) -> str:
    """Short result for the transcript; the full plan reaches the model as a reminder."""
    steps = plan["steps"]
    finished = sum(step["status"] in {"completed", "skipped"} for step in steps)
    if finished == len(steps):
        return f"Plan updated: all {len(steps)} steps finished."
    parts = [f"{finished}/{len(steps)} steps finished"]
    parts += [f"{step['id']} blocked" for step in steps if step["status"] == "blocked"]
    active = next((step for step in steps if step["status"] == "in_progress"), None)
    upcoming = next((step for step in steps if step["status"] == "pending"), None)
    if active is not None:
        parts.append(f"{active['id']} in progress")
    elif upcoming is not None:
        parts.append(f"{upcoming['id']} next")
    return f"Plan updated: {', '.join(parts)}."


def update_plan(
    steps: list[dict],
    objective: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        manager = _manager(runtime)
        before = manager.snapshot()
        plan = manager.apply_steps(steps, objective=objective)
    except Exception as e:
        return _plan_failure(e)
    ack = _acknowledge(plan)
    if plan["generation"] != before["generation"] and before["status"] not in {"empty", "completed"}:
        ack = ack.replace("Plan updated:", "New plan started (the previous unfinished plan was replaced):", 1)
    return ToolResult.success(ack)


update_plan_tool = Tool(
    name="update_plan",
    description=(
        "Write the task plan as a list of steps with their status. Use it when the "
        "work has several steps, multiple tools, or needs progress tracking; skip it "
        "for a one-step question. Mark a step in_progress when you start it and "
        "completed as soon as it is done. At most one step is in_progress, and steps "
        "finish in order; a blocked step does not hold later steps. Steps match the "
        "current plan by id when given (pass the id to rename a step), otherwise by "
        "title. Steps you leave out keep their status, so drop a step by marking it "
        "skipped. Completed steps are final; skipped ones can reopen. objective is "
        "required to start a plan. To replace the plan, send a different objective "
        "with all-new steps; a different objective with existing steps only renames "
        "the goal, so omit objective on ordinary updates. An unfinished plan carries "
        "into the next user turn, where you resume it or replace it. The current "
        "plan is attached to every model request."
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
                "description": "Steps in execution order; omitted steps are kept unchanged",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Existing step id, such as step_2; omit for a new step"},
                        "title": {"type": "string", "description": "Short step title"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "blocked", "skipped"],
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
