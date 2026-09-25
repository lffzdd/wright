"""Sub-Agent execution adapter backed by the shared Agent control plane."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...domain.model.agent import AgentProfile
from ...domain.model.coordination import AgentControlError, AgentTaskRecord
from ...domain.model.session import Session, UsageRecord
from ...infrastructure.llm.llm import LLMClient
from ...domain.policy import PermissionResolver
from ..tool_capabilities import assemble_tool_capabilities
from ...domain.model.tool import ToolAccess, ToolResult
from ...infrastructure.tools.autonomy_tools import autonomy_tools
from ...infrastructure.tools.base import Tool
from ...infrastructure.tools.runtime import ToolRuntime
from ...infrastructure.tools.task_tools import task_tools
from ...interfaces.ui_events import EventPublisher, EventScope, SessionEvents
from .runner import (
    Agent,
    assemble_agent_components,
    ensure_system_prompt,
    events_from_renderer,
    prepare_model_tools,
)

DEFAULT_CHILD_MAX_STEPS = 20
# Interactive default: root spawns leaves only. Nested spawn stays available
# when a caller passes a higher max_depth (durable runs use 2).
DEFAULT_MAX_DEPTH = 1
DEFAULT_CHILD_TIMEOUT = 300.0


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
    "Hand a self-contained subtask to a child subagent with an isolated context. "
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
            "get_task", "wait_task", "cancel_task", "list_tasks",
            "schedule_task", "get_schedule", "list_schedules",
            "pause_schedule", "resume_schedule", "cancel_schedule",
            "list_task_runs",
            "load_skill",
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
    authorization_commit_factory=None,
    publisher: EventPublisher | None = None,
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
        capabilities = runtime.capabilities
        delegation = capabilities.delegation if capabilities is not None else None
        execution = runtime.execution
        if (
            capabilities is None
            or execution is None
            or delegation is None
        ):
            return ToolResult.fail("spawn_agent requires delegation capability")
        control = delegation.control
        if run_in_background and capabilities.scope.agent_task_id is not None:
            return ToolResult.fail("Child Agents cannot launch background Agents")
        child_depth = depth + 1
        effective_max_depth = min(max_depth, control.config.max_depth)
        root_turn_id = capabilities.scope.root_turn_id
        if not root_turn_id:
            return ToolResult.fail("spawn_agent requires an active run scope")
        access_scope = runtime.access_scope
        if access_scope is None:
            return ToolResult.fail("spawn_agent requires an execution access scope")
        if execution.environment_id != "local":
            return ToolResult.fail(
                f"spawn_agent does not support execution environment {execution.environment_id}"
            )
        try:
            parent_cwd = execution.cwd()
        except Exception as exc:
            return ToolResult.fail(
                f"spawn_agent could not read the parent cwd: {type(exc).__name__}: {exc}"
            )
        if parent_cwd.environment_id != "local":
            return ToolResult.fail(
                f"spawn_agent does not support cwd environment {parent_cwd.environment_id}"
            )
        try:
            record = control.begin_task(
                root_turn_id=root_turn_id,
                parent_id=capabilities.scope.agent_task_id,
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
            authorization_commit_factory=(
                delegation.authorization_commit_factory
                or authorization_commit_factory
            ),
            publisher=publisher,
        )

        workspace_dir = access_scope.origin
        child_session = Session.create(
            initial_goal=task,
            workspace_dir=workspace_dir,
            max_steps=record.step_budget,
            # A child receives the parent's current session roots, but not any
            # one-call InvocationGrant that may have led to this delegation.
            additional_working_directories=list(access_scope.additional),
        )
        child_session.control_plane = control
        child_session.agent_task_id = record.id
        child_session.agent_root_turn_id = root_turn_id
        try:
            child_session.set_cwd(Path(parent_cwd.value))
        except Exception as exc:
            finished = control.finish_task(
                record.id,
                status="failed",
                steps_used=0,
                error=f"could not set child cwd: {type(exc).__name__}: {exc}",
            )
            _emit(runtime, finished)
            return ToolResult.fail(finished.error, data={"task_id": finished.id})
        control.bind_child_session(record.id, child_session.session_id)

        child_journal = None
        journal_factory = delegation.execution_journal_factory
        if journal_factory is not None:
            try:
                child_journal = journal_factory(
                    record.id, capabilities.scope.agent_task_id or ""
                )
            except Exception as exc:
                finished = control.finish_task(
                    record.id, status="failed", steps_used=0,
                    error=f"could not create child execution journal: {exc}",
                )
                _emit(runtime, finished)
                return ToolResult.fail(finished.error, data={"task_id": finished.id})

        child_events = (
            SessionEvents(
                publisher,
                scope=EventScope(depth=child_depth, task_id=record.id),
            )
            if render_subagents and publisher is not None
            else None
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

        child_authorization_commit = None
        child_commit_factory = (
            delegation.authorization_commit_factory or authorization_commit_factory
        )
        if child_commit_factory is not None:
            child_authorization_commit = child_commit_factory(child_session)

        child_assembly = assemble_tool_capabilities(
            child_session,
            None,
            None,
            execution_journal_factory=journal_factory,
            authorization_commit_factory=child_commit_factory,
        )
        child_profile = AgentProfile(
            "child", frozenset(tool.name for tool in child_tools),
            max_steps=record.step_budget, allow_delegation=child_depth < max_depth,
        )
        child_prepared = prepare_model_tools(child_session, child_tools, child_profile)
        ensure_system_prompt(child_session, child_prepared, None)
        child_events_for_agent = (
            child_events
            if child_events is not None
            else events_from_renderer(child_session, None)
        )
        child_components = assemble_agent_components(
            session_state=child_session,
            events=child_events_for_agent,
            prepared=child_prepared,
            assembly=child_assembly,
            permission_resolver=permission_resolver,
            cancellation_check=cancelled,
            allow_background_tasks=False,
            lifecycle=runtime.lifecycle,
            execution_journal=child_journal,
            authorization_commit=child_authorization_commit,
        )
        child_agent = Agent(
            llm,
            child_session,
            child_events_for_agent,
            child_prepared,
            child_assembly,
            components=child_components,
            max_consecutive_invalid=3,
            usage_observer=observe_usage,
            lifecycle=runtime.lifecycle,
            services=None,
            execution_journal=child_journal,
            execution_journal_factory=journal_factory,
            authorization_commit=child_authorization_commit,
            authorization_commit_factory=child_commit_factory,
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
                    error = (f"Child Agent did not finish (status={child_session.current_run_status()}, "
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
                "task_status": finished.status, "status": child_session.current_run_status(),
                "steps": child_session.step_count, "step_budget": finished.step_budget,
                "usage": usage, "children": list(finished.children),
            }
            if finished.status != "completed":
                return ToolResult.fail(error or finished.error, data=common)
            return ToolResult.success({**common, "result": finished.result})

        if run_in_background:
            background_runtime = delegation.agent_background
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
        access_descriptor=lambda args: ToolAccess(
            frozenset({"execution_control"}),
            subject=str(args.get("task") or ""),
            risk_flags=("starts_agent_execution",),
            reason="start a child Agent execution",
        ),
        is_concurrency_safe=lambda args: True,
        execution_timeout=child_timeout,
        required_capabilities=frozenset({"execution", "delegation"}),
    )


def _get_agent_tree(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    capabilities = runtime.capabilities
    delegation = capabilities.delegation if capabilities is not None else None
    if capabilities is None or delegation is None:
        return ToolResult.fail("get_agent_tree requires delegation capability")
    include_all = bool(arguments.get("include_all_turns", False))
    return ToolResult.success({
        "root_turn_id": capabilities.scope.root_turn_id,
        "limits": delegation.control.config.to_dict(),
        "tasks": delegation.control.tree_summary(
            None if include_all else capabilities.scope.root_turn_id
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
    access_descriptor=lambda args: ToolAccess.internal_read(
        reason="read the Agent control-plane tree"
    ),
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"delegation"}),
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
    authorization_commit_factory=None,
    enable_autonomy: bool = False,
    publisher: EventPublisher | None = None,
) -> list[Tool]:
    if depth < 0 or max_depth < 1 or depth > max_depth:
        raise ValueError("需要满足 0 <= depth <= max_depth 且 max_depth >= 1")
    tools = list(base_tools)
    if depth < max_depth:
        tools.append(
            make_spawn_agent_tool(
                llm,
                base_tools,
                depth=depth,
                max_depth=max_depth,
                child_max_steps=child_max_steps,
                child_timeout=child_timeout,
                render_subagents=render_subagents,
                permission_resolver=permission_resolver,
                authorization_commit_factory=authorization_commit_factory,
                publisher=publisher,
            )
        )
    # 只有 root 读取全树；子 Agent 只通过自己的 spawn 结果观察直接孩子。
    if depth == 0:
        tools.extend(task_tools)
        if enable_autonomy:
            tools.extend(replace(tool, defer_to_model=True) for tool in autonomy_tools)
        tools.append(get_agent_tree_tool)
    return tools
