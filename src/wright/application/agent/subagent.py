"""Sub-Agent execution adapter backed by the shared Agent control plane."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...domain.model.agent import AgentProfile
from ...domain.model.agent.control import AgentControlError, AgentTaskRecord
from ...domain.model.llm import UsageRecord
from ...domain.model.session import Session
from ...domain.model.tool import ToolAccess, ToolResult
from ...domain.policy import PermissionResolver
from ...infrastructure.llm.llm import LLMClient
from ...infrastructure.tools.agent_tools import (
    agent_execution_view,
    agent_tools,
    public_agent_tree,
)
from ...infrastructure.tools.autonomy_tools import autonomy_tools
from ...infrastructure.tools.base import Tool
from ...infrastructure.tools.command.control import command_tools
from ...infrastructure.tools.runtime import ToolRuntime
from ..command.execution import CommandExecution
from ..composition.roles import tools_for_role
from ..execution.identity import bind_identity
from ..session.events import EventScope, SessionEvents
from ..session.live_resources import RuntimeResources
from ..session.publisher import EventPublisher, open_session_events
from ..tool_execution.capabilities import assemble_tool_capabilities
from .assembly import (
    assemble_agent_components,
    ensure_system_prompt,
    prepare_model_tools,
)
from .runner import Agent

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
            "description": (
                "If true, launch the isolated agent and return its agent_task_id "
                "immediately. agent_task_id identifies this delegation, not a "
                "reusable Agent instance."
            ),
        },
    },
    "required": ["task"],
    "additionalProperties": False,
}

SPAWN_AGENT_DESCRIPTION = (
    "Hand a self-contained subtask to a child subagent with an isolated context. "
    "Consecutive spawn_agent calls may run concurrently; the control plane "
    "records agent_task_id, parent/child links, status, budget, and usage. "
    "agent_task_id identifies this delegation, not a reusable Agent. "
    "Use get_agent, wait_agent, or cancel_agent with that id. "
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
        if run_in_background and capabilities.scope.agent_task_id is not None:
            return ToolResult.fail("Child Agents cannot launch background Agents")
        child_depth = depth + 1
        effective_max_depth = min(max_depth, delegation.limits()["max_depth"])
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
            record = delegation.begin_task(
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
            return ToolResult.fail(record.error, data=agent_execution_view(record))

        child_base_tools = tools_for_role(base_tools, "child")
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
        delegation.share_control_plane(child_session)
        child_session.agent_task_id = record.id
        child_session.agent_root_turn_id = root_turn_id
        try:
            child_session.set_cwd(Path(parent_cwd.value))
        except Exception as exc:
            finished = delegation.finish_task(
                record.id,
                status="failed",
                steps_used=0,
                error=f"could not set child cwd: {type(exc).__name__}: {exc}",
            )
            _emit(runtime, finished)
            return ToolResult.fail(finished.error, data=agent_execution_view(finished))
        delegation.bind_child_session(record.id, child_session.session_id)

        child_journal = None
        journal_factory = delegation.execution_journal_factory
        if journal_factory is not None:
            try:
                child_journal = journal_factory(
                    record.id, capabilities.scope.agent_task_id or ""
                )
            except Exception as exc:
                finished = delegation.finish_task(
                    record.id, status="failed", steps_used=0,
                    error=f"could not create child execution journal: {exc}",
                )
                _emit(runtime, finished)
                return ToolResult.fail(finished.error, data=agent_execution_view(finished))

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
                delegation.request_cancel(record.id, reason)
                return True
            return delegation.is_cancelled(record.id)

        def observe_usage(usage: UsageRecord) -> None:
            delegation.add_usage(
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

        child_commands = CommandExecution(
            child_session,
            bind_identity(child_session),
            allow_background=False,
        )
        child_assembly = assemble_tool_capabilities(
            child_session,
            None,
            RuntimeResources(child_session.session_id, commands=child_commands),
            execution_journal_factory=journal_factory,
            authorization_commit_factory=child_commit_factory,
        )
        child_profile = AgentProfile(
            "child", frozenset(tool.name for tool in child_tools),
            max_steps=record.step_budget, allow_delegation=child_depth < max_depth,
        )
        child_prepared = prepare_model_tools(child_session, child_tools, child_profile)
        ensure_system_prompt(child_session, child_prepared, None)
        child_events_for_agent = child_events or open_session_events(child_session)
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
                return _run_child_body()
            finally:
                child_commands.close()

        def _run_child_body() -> ToolResult:
            try:
                final_answer = child_agent.run(task, max_steps=record.step_budget)
                cancellation_reason = delegation.cancellation_reason(record.id)
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

            finished = delegation.finish_task(
                record.id, status=task_status, steps_used=child_session.step_count,
                result=final_answer or "", error=error,
            )
            _emit(runtime, finished)
            view = agent_execution_view(finished)
            if finished.status != "completed":
                return ToolResult.fail(error or finished.error, data=view)
            return ToolResult.success(view)

        if run_in_background:
            if delegation.submit_background is None:
                child_commands.close()
                finished = delegation.finish_task(
                    record.id, status="failed", steps_used=0,
                    error="Current session has no background Agent runtime",
                )
                _emit(runtime, finished)
                return ToolResult.fail(finished.error, data=agent_execution_view(finished))
            try:
                delegation.submit_background(record.id, run_child)
            except Exception as exc:
                child_commands.close()
                finished = delegation.finish_task(
                    record.id, status="failed", steps_used=0, error=str(exc)
                )
                _emit(runtime, finished)
                return ToolResult.fail(finished.error, data=agent_execution_view(finished))
            launched = agent_execution_view(record)
            launched["message"] = (
                "Delegation is running in the background. Use get_agent, "
                "wait_agent, or cancel_agent with this agent_task_id."
            )
            return ToolResult.success(launched)
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
        "limits": delegation.limits(),
        "tasks": public_agent_tree(delegation.tree(
            None if include_all else capabilities.scope.root_turn_id
        )),
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
        tools.extend(agent_tools)
        tools.extend(command_tools)
        if enable_autonomy:
            tools.extend(replace(tool, defer_to_model=True) for tool in autonomy_tools)
        tools.append(get_agent_tree_tool)
    return tools
