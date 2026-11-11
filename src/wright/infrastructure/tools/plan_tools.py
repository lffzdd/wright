"""Expose the session PlanManager as model-callable tools."""

from __future__ import annotations

from ...domain.feedback import system_text
from ...domain.model.planning.plan import PlanError
from ...domain.model.tool import ToolAccess, ToolResult
from ...domain.model.tool_definition import Tool
from ...domain.model.tool_runtime import ToolRuntime
from .system_result import fail_text


def _plan_failure(exc: Exception, manager=None) -> ToolResult:
    if isinstance(exc, PlanError) and exc.code:
        result = fail_text(system_text(exc.code, str(exc), **exc.params))
        if manager is not None:
            # A rule rejection leaves the plan as it was; show that plan and
            # which rule refused the change, so the card is not just the error.
            result.display = {
                **_plan_view(manager.snapshot()),
                "rejected": {"code": exc.code, "params": dict(exc.params)},
            }
        return result
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
    done = sum(step["status"] == "completed" for step in steps)
    skipped = sum(step["status"] == "skipped" for step in steps)
    if done == len(steps):
        return f"Plan updated: all {len(steps)} steps done."
    # Skipped steps close the plan but were not done; report them apart.
    parts = [f"{done}/{len(steps)} steps done"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if done + skipped == len(steps):
        return f"Plan updated: plan finished, {', '.join(parts)}."
    parts += [f"{step['id']} blocked" for step in steps if step["status"] == "blocked"]
    active = next((step for step in steps if step["status"] == "in_progress"), None)
    upcoming = next((step for step in steps if step["status"] == "pending"), None)
    if active is not None:
        parts.append(f"{active['id']} in progress")
    elif upcoming is not None:
        parts.append(f"{upcoming['id']} next")
    return f"Plan updated: {', '.join(parts)}."


def _plan_view(plan: dict) -> dict:
    """The plan as the transcript card shows it; the model gets only the acknowledgement."""
    return {
        "objective": plan.get("objective", ""),
        "steps": [
            {key: step.get(key, "") for key in ("id", "title", "status", "note")}
            for step in plan["steps"]
        ],
    }


def update_plan(
    steps: list[dict],
    objective: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    manager = None
    try:
        manager = _manager(runtime)
        before = manager.snapshot()
        plan = manager.apply_steps(steps, objective=objective)
    except Exception as e:
        return _plan_failure(e, manager)
    ack = _acknowledge(plan)
    if plan["generation"] != before["generation"] and before["status"] not in {"empty", "completed"}:
        ack = ack.replace("Plan updated:", "New plan started (the previous unfinished plan was replaced):", 1)
    return ToolResult.success(ack, display=_plan_view(plan))


update_plan_tool = Tool(
    name="update_plan",
    description=(
        "Write the task plan as a list of steps with their status. Use it when the "
        "work has several steps, multiple tools, or needs progress tracking; skip it "
        "for a one-step question. Mark a step in_progress when you start it and "
        "completed as soon as it is done. At most one step is in_progress; steps "
        "may finish in any order. Steps match the "
        "current plan by id when given (pass the id to rename a step), otherwise by "
        "title. Steps you leave out keep their status, so drop a step by marking it "
        "skipped. If a step fails and you stop trying, mark it skipped with a note "
        "saying why; keep it in_progress while you retry, and use blocked when it "
        "waits on the user or something outside your control. Completed steps are "
        "final; skipped ones can reopen. objective is "
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
