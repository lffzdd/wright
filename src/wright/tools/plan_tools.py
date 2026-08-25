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
        "为当前复杂任务建立一个简洁、可执行的步骤计划。适用于需要多个独立步骤、"
        "会使用多个工具或需要持续跟踪进度的任务；简单的一步问题不必创建计划。"
        "已有未完成计划时默认拒绝覆盖，只有明确放弃旧计划才传 replace=true。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "这份计划要达成的明确目标",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 12,
                "description": "按执行顺序排列的简洁步骤标题",
            },
            "replace": {
                "type": "boolean",
                "default": False,
                "description": "是否明确替换当前尚未完成的整份计划",
            },
        },
        "required": ["objective", "steps"],
    },
    call=lambda args, runtime: create_plan(**args, runtime=runtime),
)

update_plan_tool = Tool(
    name="update_plan",
    description=(
        "更新一个计划步骤的状态。开始步骤时标为 in_progress，做完立即标为 completed；"
        "无法继续时标为 blocked 并在 note 说明原因。任何时刻最多一个步骤 in_progress。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "step_id": {
                "type": "string",
                "description": "create_plan/get_plan 返回的步骤 id，如 step_1",
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
                "description": "步骤的新状态",
            },
            "note": {
                "type": "string",
                "description": "可选的进展、结果或阻塞原因",
            },
        },
        "required": ["step_id", "status"],
    },
    call=lambda args, runtime: update_plan(**args, runtime=runtime),
)

get_plan_tool = Tool(
    name="get_plan",
    description="读取当前任务的完整计划、整体状态、revision 和所有步骤。",
    parameters={"type": "object", "properties": {}, "required": []},
    call=lambda args, runtime: get_plan(runtime=runtime),
    is_concurrency_safe=lambda args: True,
)

replan_tool = Tool(
    name="replan",
    description=(
        "当事实、约束或执行结果导致原路线不再适用时重规划。已完成/已跳过步骤保留为历史，"
        "旧的未完成步骤会标为 skipped，并追加新的待执行步骤。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 12,
                "description": "新的后续执行步骤",
            },
            "reason": {
                "type": "string",
                "description": "为什么原路线需要调整",
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
