"""把 SessionState 内的 PlanManager 暴露成模型可调用工具。"""

from __future__ import annotations

from .base import Tool, ToolResult, ToolRuntime


def _manager(runtime: ToolRuntime | None):
    session = runtime.session_state if runtime is not None else None
    manager = getattr(session, "plan_manager", None)
    if manager is None:
        raise RuntimeError("plan tool requires a SessionState with PlanManager")
    return manager


def create_plan(
    objective: str,
    steps: list[str],
    replace: bool = False,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(
            _manager(runtime).create_plan(objective, steps, replace=replace)
        )
    except Exception as e:
        return ToolResult.fail(str(e))


def update_plan(
    step_id: str,
    status: str,
    note: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(
            _manager(runtime).update_step(step_id, status, note=note)
        )
    except Exception as e:
        return ToolResult.fail(str(e))


def get_plan(runtime: ToolRuntime | None = None) -> ToolResult:
    try:
        return ToolResult.success(_manager(runtime).snapshot())
    except Exception as e:
        return ToolResult.fail(str(e))


def replan(
    steps: list[str],
    reason: str,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(_manager(runtime).replan(steps, reason=reason))
    except Exception as e:
        return ToolResult.fail(str(e))


create_plan_tool = Tool(
    name="create_plan",
    description=(
        "Create a concise, executable step plan for the current complex task. Use it when "
        "the work has several independent steps, multiple tools, or needs progress tracking. "
        "Skip it for a one-step question. An unfinished plan is not replaced unless replace=true."
    ),
    parameters={
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "The clear goal this plan should achieve",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 12,
                "description": "Short step titles in execution order",
            },
            "replace": {
                "type": "boolean",
                "default": False,
                "description": "Whether to replace the current unfinished plan",
            },
        },
        "required": ["objective", "steps"],
    },
    call=lambda args, runtime: create_plan(**args, runtime=runtime),
)

update_plan_tool = Tool(
    name="update_plan",
    description=(
        "Update one plan step. Mark it in_progress when you start, completed as soon as it "
        "is done, or blocked with a note if it cannot continue. At most one step may be "
        "in_progress at a time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "step_id": {
                "type": "string",
                "description": "Step id from create_plan/get_plan, e.g. step_1",
            },
            "status": {
                "type": "string",
                "enum": [
                    "pending",
                    "in_progress",
                    "completed",
                    "blocked",
                    "skipped",
                ],
                "description": "New step status",
            },
            "note": {
                "type": "string",
                "description": "Optional progress, result, or blocker",
            },
        },
        "required": ["step_id", "status"],
    },
    call=lambda args, runtime: update_plan(**args, runtime=runtime),
)

get_plan_tool = Tool(
    name="get_plan",
    description="Read the current task plan: overall status, revision, and all steps.",
    parameters={"type": "object", "properties": {}, "required": []},
    call=lambda args, runtime: get_plan(runtime=runtime),
    is_concurrency_safe=lambda args: True,
)

replan_tool = Tool(
    name="replan",
    description=(
        "Replan when new facts, constraints, or results make the current route "
        "obsolete. Completed and skipped steps are kept as history, remaining "
        "unfinished steps are marked skipped, and the new steps are appended."
    ),
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 12,
                "description": "The new steps to execute from here on.",
            },
            "reason": {
                "type": "string",
                "description": "Why the current route needs to change.",
            },
        },
        "required": ["steps", "reason"],
    },
    call=lambda args, runtime: replan(**args, runtime=runtime),
)


plan_tools: list[Tool] = [
    create_plan_tool,
    update_plan_tool,
    get_plan_tool,
    replan_tool,
]
