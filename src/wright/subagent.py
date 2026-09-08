"""Sub-Agent execution adapter backed by the shared Agent control plane."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console

from .agent import Agent
from .coordination import AgentControlError, AgentTaskRecord
from .llm import LLMClient
from .logger import get_logger
from .permission import PermissionResolver
from .renderer import Renderer, SilentRenderer
from .session import SessionState, UsageRecord
from .tools.autonomy_tools import autonomy_tools
from .tools.base import Tool, ToolResult, ToolRuntime
from .tools.task_tools import task_tools

logger = get_logger(__name__)


DEFAULT_CHILD_MAX_STEPS = 20
# Interactive default: root spawns leaves only. Nested spawn stays available
# when a caller passes a higher max_depth (durable runs use 2).
DEFAULT_MAX_DEPTH = 1
DEFAULT_CHILD_TIMEOUT = 300.0


class SubAgentRenderer(Renderer):
    """Compact renderer for an isolated child context."""

    def __init__(self, depth: int, task: str) -> None:
        self.depth = depth
        self.task = task
        self._prefix = "    " * (depth - 1) + "│ "
        self._console = Console(highlight=False)

    def _line(self, text: str, style: str = "") -> None:
        try:
            from .tui.renderer import TUIRenderer

            if TUIRenderer.is_active():
                return
        except Exception:
            pass
        self._console.print(
            f"{self._prefix}{text}", style=style, highlight=False, markup=False
        )

    def on_reasoning_delta(self, piece: str) -> None: ...
    def on_content_delta(self, piece: str) -> None: ...
    def on_command_output(self, line: str) -> None: ...

    def on_tool_call(self, tool_call) -> None:
        args = getattr(tool_call, "arguments", {}) or {}
        brief = json.dumps(args, ensure_ascii=False)
        if len(brief) > 80:
            brief = brief[:77] + "..."
        self._line(f"🔧 子Agent(d{self.depth}) › {tool_call.name} {brief}", "yellow")

    def on_tool_result(self, tool_call, tool_result) -> None:
        name = getattr(tool_call, "name", None) or "tool"
        if hasattr(tool_result, "to_dict"):
            tool_result = tool_result.to_dict()
        if tool_result.get("ok"):
            self._line(f"✅ 子Agent(d{self.depth}) › {name}", "green")
        else:
            self._line(
                f"❌ 子Agent(d{self.depth}) › {name}: {tool_result.get('err')}",
                "red",
            )

    def on_agent_event(self, event: dict[str, Any]) -> None:
        self._line(
            f"↳ agent {event.get('task_id')} · d{event.get('depth')} · "
            f"{event.get('status')}",
            "cyan" if event.get("status") == "running" else "green",
        )

    def on_final(self, answer) -> None:
        text = answer if isinstance(answer, str) else json.dumps(
            answer, ensure_ascii=False
        )
        if len(text) > 200:
            text = text[:197] + "..."
        self._line(f"🎯 子Agent(d{self.depth}) 收口: {text}", "green")

    def on_completion_rejected(self, issues=()) -> None:
        detail = ""
        if issues:
            first = issues[0]
            detail = getattr(first, "message", None) or (
                first.get("message") if isinstance(first, dict) else ""
            )
        self._line(
            f"完成检查未通过: {detail or '未说明原因'}",
            "yellow",
        )


SPAWN_AGENT_PARAMETERS = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4_000,
            "description": (
                "A self-contained subtask. The child Agent cannot see parent "
                "history, so include full background, goal, constraints, and "
                "expected output."
            ),
        },
        "run_in_background": {
            "type": "boolean",
            "default": False,
            "description": "If true, launch the isolated agent and return its task_id immediately.",
        },
    },
    "required": ["task"],
    "additionalProperties": False,
}

SPAWN_AGENT_DESCRIPTION = (
    "Hand a self-contained subtask to a child Agent with an isolated context. "
    "Consecutive spawn_agent calls may run concurrently; the control plane "
    "records task_id, parent/child links, status, budget, and usage. "
    "Child Agents share the workspace and permission boundary, but not parent "
    "history, long-term memory, or ask_user. "
    "run_in_background=true is root-only; completion notifies the parent session."
)


def _emit(runtime: ToolRuntime, record: AgentTaskRecord) -> None:
    if runtime.emit_progress is not None:
        runtime.emit_progress({
            "type": "agent_task",
            "task_id": record.id,
            "parent_id": record.parent_id,
            "depth": record.depth,
            "task": record.task,
            "status": record.status,
            "steps": record.steps_used,
            "usage": record.total_tokens,
        })


def _child_base_tools(base_tools: Sequence[Tool]) -> list[Tool]:
    """Remove capabilities whose lifecycle cannot outlive an isolated child."""
    child_tools: list[Tool] = []
    for tool in base_tools:
        if tool.name in {
            "get_task_output", "get_agent_task", "cancel_agent_task",
            "get_task", "wait_task", "cancel_task", "list_tasks",
            "create_task", "get_schedule", "list_schedules",
            "pause_schedule", "resume_schedule", "cancel_schedule",
            "list_task_runs",
            "skill",
        }:
            continue
        if tool.name == "execute_command":
            parameters = deepcopy(tool.parameters)
            properties = parameters.get("properties", {})
            if isinstance(properties, dict):
                properties["run_in_background"] = {
                    "type": "boolean",
                    "const": False,
                    "default": False,
                    "description": "Must be false; child Agents cannot leave background processes",
                }
            child_tools.append(replace(
                tool,
                description=(
                    "Execute a foreground shell command in the shared workspace. "
                    "Sub-Agents cannot create or retain background processes."
                ),
                parameters=parameters,
            ))
            continue
        child_tools.append(tool)
    return child_tools


def make_spawn_agent_tool(
    llm: LLMClient,
    base_tools: Sequence[Tool],
    *,
    depth: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    child_max_steps: int = DEFAULT_CHILD_MAX_STEPS,
    child_timeout: float = DEFAULT_CHILD_TIMEOUT,
    render_subagents: bool = True,
    permission_resolver: PermissionResolver | None = None,
) -> Tool:
    if depth < 0 or max_depth < 1 or depth >= max_depth:
        raise ValueError("spawn_agent 只能在 0 <= depth < max_depth 时创建")
    if child_max_steps < 1:
        raise ValueError("child_max_steps 必须 > 0")
    if child_timeout <= 0:
        raise ValueError("child_timeout 必须 > 0")

    def _call(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
        task = arguments["task"].strip()
        run_in_background = bool(arguments.get("run_in_background", False))
        session = runtime.session_state
        if session is None:
            return ToolResult.fail("spawn_agent requires SessionState runtime")
        control = session.control_plane
        if run_in_background and session.agent_task_id is not None:
            return ToolResult.fail("Child Agents cannot launch background Agents")
        child_depth = depth + 1
        effective_max_depth = min(max_depth, control.config.max_depth)
        root_turn_id = session.agent_root_turn_id or (
            f"{session.session_id}:{session.active_turn_start_message_index}"
        )
        try:
            record = control.begin_task(
                root_turn_id=root_turn_id,
                parent_id=session.agent_task_id,
                tool_call_id=runtime.tool_call_id,
                depth=child_depth,
                task=task,
                requested_steps=child_max_steps,
                max_depth=effective_max_depth,
            )
        except AgentControlError as exc:
            return ToolResult.fail(
                f"AgentControlError: {exc}",
                data={"status": "rejected", "reason": str(exc)},
            )

        _emit(runtime, record)
        if record.status != "running":
            return ToolResult.fail(
                record.error,
                data={
                    "task_id": record.id,
                    "status": record.status,
                    "reason": record.error,
                },
            )

        child_base_tools = _child_base_tools(base_tools)
        child_tools = build_agent_tools(
            llm,
            child_base_tools,
            depth=child_depth,
            max_depth=effective_max_depth,
            child_max_steps=child_max_steps,
            child_timeout=child_timeout,
            render_subagents=render_subagents,
            permission_resolver=permission_resolver,
        )

        workspace_dir = runtime.workspace_dir or Path.cwd()
        child_session = SessionState.create(
            user_goal=task,
            workspace_dir=workspace_dir,
            max_steps=record.step_budget,
        )
        child_session.control_plane = control
        child_session.agent_task_id = record.id
        child_session.agent_root_turn_id = root_turn_id
        if runtime.cwd_provider is not None:
            try:
                child_session.set_cwd(runtime.cwd_provider())
            except Exception:
                logger.debug("child session set_cwd failed", exc_info=True)
        control.bind_child_session(record.id, child_session.session_id)

        child_renderer: Renderer = (
            SubAgentRenderer(child_depth, task)
            if render_subagents else SilentRenderer()
        )

        def cancelled() -> bool:
            if runtime.is_cancelled():
                reason = runtime.get_cancellation_reason() or "parent_cancelled"
                control.request_cancel(record.id, reason)
                return True
            return control.is_cancelled(record.id)

        def observe_usage(usage: UsageRecord) -> None:
            control.add_usage(
                record.id,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
            )

        child_agent = Agent(
            llm,
            child_tools,
            child_session,
            child_renderer,
            max_consecutive_invalid=3,
            permission_resolver=permission_resolver,
            cancellation_check=cancelled,
            usage_observer=observe_usage,
            allow_background_tasks=False,
            lifecycle=runtime.lifecycle,
            services=runtime.services,
        )

        def run_child() -> ToolResult:
            try:
                final_answer = child_agent.run(task, max_steps=record.step_budget)
                cancellation_reason = control.cancellation_reason(record.id)
                runtime_reason = runtime.get_cancellation_reason()
                if runtime_reason == "timeout":
                    task_status, error = "timed_out", "Child Agent exceeded parent tool deadline"
                elif cancellation_reason or runtime.is_cancelled():
                    task_status = "cancelled"
                    error = cancellation_reason or runtime_reason or "Child Agent cancelled"
                elif final_answer is None:
                    task_status = "failed"
                    error = (f"Child Agent did not finish (status={child_session.status}, "
                             f"steps={child_session.step_count}/{record.step_budget})")
                else:
                    task_status, error = "completed", ""
            except Exception as exc:
                final_answer = None
                task_status, error = "failed", f"Child Agent error: {type(exc).__name__}: {exc}"

            usage = {
                "prompt_tokens": child_session.total_usage.prompt_tokens,
                "completion_tokens": child_session.total_usage.completion_tokens,
                "total_tokens": child_session.total_usage.total_tokens,
            }
            finished = control.finish_task(
                record.id, status=task_status, steps_used=child_session.step_count,
                result=final_answer or "", error=error,
            )
            _emit(runtime, finished)
            common = {
                "task_id": finished.id, "parent_id": finished.parent_id,
                "task_status": finished.status, "status": child_session.status,
                "steps": child_session.step_count, "step_budget": finished.step_budget,
                "usage": usage, "children": list(finished.children),
            }
            if finished.status != "completed":
                return ToolResult.fail(error or finished.error, data=common)
            return ToolResult.success({**common, "result": finished.result})

        if run_in_background:
            background_runtime = (
                runtime.services.agent_background if runtime.services else None
            )
            if background_runtime is None:
                finished = control.finish_task(
                    record.id, status="failed", steps_used=0,
                    error="Current session has no background Agent runtime",
                )
                _emit(runtime, finished)
                return ToolResult.fail(finished.error, data={"task_id": finished.id})
            try:
                background_runtime.submit(record.id, run_child, control)
            except Exception as exc:
                finished = control.finish_task(
                    record.id, status="failed", steps_used=0, error=str(exc)
                )
                _emit(runtime, finished)
                return ToolResult.fail(finished.error, data={"task_id": finished.id})
            return ToolResult.success({
                "task_id": record.id, "parent_id": record.parent_id,
                "task_status": "async_launched", "status": "running",
                "step_budget": record.step_budget,
            })
        return run_child()

    return Tool(
        name="spawn_agent",
        description=SPAWN_AGENT_DESCRIPTION,
        parameters=SPAWN_AGENT_PARAMETERS,
        call=_call,
        is_concurrency_safe=lambda args: True,
        execution_timeout=child_timeout,
    )


def _get_agent_tree(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    session = runtime.session_state
    if session is None:
        return ToolResult.fail("get_agent_tree requires SessionState runtime")
    include_all = bool(arguments.get("include_all_turns", False))
    return ToolResult.success({
        "root_turn_id": session.agent_root_turn_id,
        "limits": session.control_plane.config.to_dict(),
        "tasks": session.control_plane.tree_summary(
            None if include_all else session.agent_root_turn_id
        ),
    })


get_agent_tree_tool = Tool(
    name="get_agent_tree",
    description=(
        "Read the child-Agent control-plane tree: lifecycle, usage, and result summaries. "
        "Defaults to the current user turn; include all turns when debugging history."
    ),
    parameters={
        "type": "object",
        "properties": {"include_all_turns": {"type": "boolean", "default": False}},
        "required": [],
        "additionalProperties": False,
    },
    call=_get_agent_tree,
    is_concurrency_safe=lambda args: True,
)


def _get_agent_task(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    session = runtime.session_state
    if session is None:
        return ToolResult.fail("get_agent_task requires SessionState runtime")
    task_id = str(arguments["task_id"])
    try:
        from .tasks import TaskNotFoundError, TaskService

        task = TaskService.for_session(session, runtime.services).get(task_id)
    except TaskNotFoundError as exc:
        return ToolResult.fail(str(exc))
    if task.kind != "agent":
        return ToolResult.fail(f"Task is not an Agent task: {task_id}")
    return ToolResult.success(dict(task.details))


get_agent_task_tool = Tool(
    name="get_agent_task",
    description=(
        "Compatibility alias for Agent tasks. Prefer get_task, which supports "
        "both Agent and shell tasks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
    call=_get_agent_task,
    is_concurrency_safe=lambda args: True,
    expose_to_model=False,
)


def _cancel_agent_task(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    session = runtime.session_state
    if session is None:
        return ToolResult.fail("cancel_agent_task requires SessionState runtime")
    task_id = str(arguments["task_id"])
    reason = str(arguments.get("reason") or "Root Agent requested cancellation")[:1_000]
    try:
        from .tasks import TaskNotFoundError, TaskService

        service = TaskService.for_session(session, runtime.services)
        before = service.get(task_id)
        if before.kind != "agent":
            return ToolResult.fail(f"Task is not an Agent task: {task_id}")
        task = service.cancel(task_id, reason=reason)
    except TaskNotFoundError as exc:
        return ToolResult.fail(str(exc))
    return ToolResult.success({
        "task_id": task_id,
        "status": task.status,
        "cancel_requested": task.cancel_requested,
        "reason": task.cancel_reason,
        "already_terminal": before.terminal,
    })


cancel_agent_task_tool = Tool(
    name="cancel_agent_task",
    description=(
        "Compatibility alias for Agent tasks. Prefer cancel_task, which routes "
        "both Agent and shell cancellation through the unified task service."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "maxLength": 1_000},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
    call=_cancel_agent_task,
    is_concurrency_safe=lambda args: True,
    expose_to_model=False,
)


def build_agent_tools(
    llm: LLMClient,
    base_tools: Sequence[Tool],
    *,
    depth: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    child_max_steps: int = DEFAULT_CHILD_MAX_STEPS,
    child_timeout: float = DEFAULT_CHILD_TIMEOUT,
    render_subagents: bool = True,
    permission_resolver: PermissionResolver | None = None,
    enable_autonomy: bool = False,
) -> list[Tool]:
    if depth < 0 or max_depth < 1 or depth > max_depth:
        raise ValueError("需要满足 0 <= depth <= max_depth 且 max_depth >= 1")
    tools = list(base_tools)
    if depth < max_depth:
        tools.append(make_spawn_agent_tool(
            llm,
            base_tools,
            depth=depth,
            max_depth=max_depth,
            child_max_steps=child_max_steps,
            child_timeout=child_timeout,
            render_subagents=render_subagents,
            permission_resolver=permission_resolver,
        ))
    # 只有 root 读取全树；子 Agent 只通过自己的 spawn 结果观察直接孩子。
    if depth == 0:
        tools.extend([
            *task_tools,
            *(autonomy_tools if enable_autonomy else []),
            get_agent_tree_tool,
            get_agent_task_tool,
            cancel_agent_task_tool,
        ])
    return tools
