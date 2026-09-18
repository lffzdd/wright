"""Isolated background execution for durable scheduled runs.

The REPL thread constructs the session and submits a worker; the scheduler
thread still only writes the store and enqueues ``DURABLE_RUN_DUE``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from typing import Any

from ..agent import Agent
from ..agent_background import AgentBackgroundRuntime
from ..capabilities import AgentProfile
from ..coordination import AgentControlError, AgentControlPlane
from ..llm import LLMClient, resolve_transport
from ..permission import (
    PermissionCheckResult,
    PermissionRequest,
    PermissionResolver,
    PermissionSettings,
    RuleBasedApprovalHandler,
)
from ..renderer import SilentRenderer
from ..services import RuntimeServices
from ..session import Session, UsageRecord
from ..subagent import (
    _child_base_tools,
    build_agent_tools,
)
from ..tools.base import Tool
from .scheduler import AutonomyScheduler


class _DurableToolJournal:
    """Adapter that makes durable tool calls transactional with the SQLite log."""

    def __init__(self, scheduler: AutonomyScheduler, run_id: str) -> None:
        self.store = scheduler.store
        self.run_id = run_id

    def record_intent(self, **value: Any) -> None:
        self.store.record_tool_intent(run_id=self.run_id, **value)

    def mark_started(self, call_id: str) -> None:
        self.store.mark_tool_started(self.run_id, call_id)

    def record_result(self, call_id: str, result: dict[str, Any], *, status: str) -> None:
        self.store.record_tool_result(self.run_id, call_id, result, status=status)

# Isolated workers start at depth=1 and may spawn one leaf helper.
DURABLE_MAX_DEPTH = 2


# Durable runs must not ask a human, spawn more schedules, or write memory.
# Autonomy names are also in ``_child_base_tools``; listed here so the
# isolation policy stays explicit if that helper changes.
# knowledge_search 会消耗外部 API 额度并依赖网络；skill 加载会占用步数和上下文。
# 无人值守任务应把流程写进 prompt，而不是现场发现并加载。
_DURABLE_EXCLUDED_TOOLS = frozenset({
    "ask_user",
    "schedule_task",
    "get_schedule",
    "list_schedules",
    "pause_schedule",
    "resume_schedule",
    "cancel_schedule",
    "list_task_runs",
    "create_memory",
    "get_memory",
    "update_memory",
    "delete_memory",
    "search_memory",
    "search_episodes",
    "get_episode",
    "delete_episode",
    "knowledge_search",
    "load_skill",
})

_UNATTENDED_DENY_NOTE = (
    "durable run is unattended and cannot prompt for confirmation"
)


@dataclass(frozen=True)
class DurableLaunch:
    run_id: str
    task_id: str
    session: Session
    tool_names: tuple[str, ...]


class _UnattendedApprovalHandler:
    """Rule-based approval that can never fall through to a terminal prompt."""

    def __init__(self, settings: PermissionSettings) -> None:
        self._inner = RuleBasedApprovalHandler(settings)

    def __call__(self, request: PermissionRequest) -> PermissionCheckResult:
        result = self._inner(request)
        if result.decision == "allow":
            return result
        reason = result.reason
        if _UNATTENDED_DENY_NOTE not in reason:
            reason = f"{reason}; {_UNATTENDED_DENY_NOTE}"
        return PermissionCheckResult(
            "deny",
            reason,
            result.risk_flags,
            source=result.source or "durable_unattended",
        )


def _durable_base_tools(base_tools: Sequence[Tool]) -> list[Tool]:
    return [
        tool
        for tool in _child_base_tools(base_tools)
        if tool.name not in _DURABLE_EXCLUDED_TOOLS
    ]


def _durable_root_turn_id(run_id: str) -> str:
    return f"durable:{run_id}"


def _user_prompt_for_run(scheduler: AutonomyScheduler, run_id: str) -> str:
    event = scheduler.runtime_event(run_id)
    task = event.get("task") if isinstance(event.get("task"), dict) else {}
    prompt = str(task.get("prompt") or task.get("name") or "durable task").strip()
    trigger_type = str(task.get("trigger_type") or "")
    trigger_payload = task.get("trigger_payload") or {}
    if trigger_type in {"once", "interval"} and not trigger_payload:
        return prompt
    meta = json.dumps(
        {
            "trigger_type": trigger_type,
            "trigger_payload": trigger_payload,
            "attempt": task.get("attempt"),
            "max_retries": task.get("max_retries"),
        },
        ensure_ascii=False,
        default=repr,
    )
    return f"{prompt}\n\n<durable-trigger>\n{meta}\n</durable-trigger>"


def _fail_closed_resolver(settings: PermissionSettings) -> PermissionResolver:
    return PermissionResolver(approval_handler=_UnattendedApprovalHandler(settings))


def _commit_durable_run(
    *,
    scheduler: AutonomyScheduler,
    control: AgentControlPlane,
    run_id: str,
    task_id: str,
    session: Session,
    final_answer: str | None,
    task_status: str,
    error: str,
) -> None:
    control.finish_task(
        task_id,
        status=task_status,  # type: ignore[arg-type]
        steps_used=session.step_count,
        result=final_answer or "",
        error=error,
    )
    current = scheduler.store.get_run(run_id)
    if current.terminal:
        return
    if current.cancel_requested:
        status = "cancelled"
        error = current.cancel_reason or error or "run cancelled"
    elif task_status == "cancelled":
        status = "cancelled"
    elif task_status == "completed" and final_answer is not None:
        status, error = "completed", ""
    else:
        status = "failed"
        if not error:
            error = (
                f"autonomous Agent ended with run status={session.current_run_status()}"
            )
    scheduler.finish_run(
        run_id,
        status=status,
        result=final_answer or "",
        error=error,
    )


def launch_durable_run(
    *,
    run_id: str,
    root_session: Session | None = None,
    workspace_dir=None,
    control_plane: AgentControlPlane | None = None,
    scheduler: AutonomyScheduler,
    llm: LLMClient,
    base_tools: Sequence[Tool],
    permission_settings: PermissionSettings,
    background_runtime: AgentBackgroundRuntime,
    lifecycle: Any = None,
    services: RuntimeServices,
    max_steps: int = 50,
    max_depth: int = DURABLE_MAX_DEPTH,
) -> DurableLaunch | None:
    """Construct an isolated durable session on the REPL thread and return.

    The worker runs in the shared background pool.  Callers must not wait.
    """
    run = scheduler.store.get_run(run_id)
    if run.status != "dispatched":
        return None
    configured_steps = run.run_config.get("max_steps")
    if configured_steps is not None:
        max_steps = int(configured_steps)
    scheduler.store.start_run(run_id, owner_id=scheduler.host_id)
    root_turn_id = _durable_root_turn_id(run_id)
    scheduler.store.set_run_root_turn(run_id, root_turn_id)

    if root_session is None and workspace_dir is None:
        raise ValueError("durable run requires workspace_dir when no source Session exists")
    control = control_plane or (
        root_session.control_plane if root_session is not None else AgentControlPlane()
    )
    prompt = run.prompt.strip() or run.automation_name or "durable task"
    try:
        record = control.begin_task(
            root_turn_id=root_turn_id,
            parent_id=None,
            tool_call_id=run_id,
            depth=1,
            task=prompt,
            requested_steps=max_steps,
            max_depth=max_depth,
        )
    except AgentControlError as exc:
        scheduler.finish_run(
            run_id,
            status="failed",
            error=f"AgentControlError: {exc}",
        )
        raise

    if record.status != "running":
        scheduler.finish_run(
            run_id,
            status="failed",
            error=record.error or "control plane did not start durable task",
        )
        return None

    permission_resolver = _fail_closed_resolver(permission_settings)
    child_tools = build_agent_tools(
        llm,
        _durable_base_tools(base_tools),
        depth=1,
        max_depth=min(max_depth, control.config.max_depth),
        render_subagents=False,
        permission_resolver=permission_resolver,
        enable_autonomy=False,
    )
    child_session = Session.create(
        initial_goal=prompt,
        workspace_dir=(root_session.workspace_dir if root_session is not None else workspace_dir),
        max_steps=record.step_budget,
    )
    child_session.control_plane = control
    child_session.agent_task_id = record.id
    child_session.agent_root_turn_id = root_turn_id
    control.bind_child_session(record.id, child_session.session_id)

    def cancelled() -> bool:
        if scheduler.store.is_cancel_requested(run_id):
            reason = (
                scheduler.store.get_run(run_id).cancel_reason
                or "durable run cancelled"
            )
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

    # Automation configuration is a persisted run snapshot.  Do not mutate a
    # host-shared client when a task selected a model: concurrent durable runs
    # must retain their own selection.
    run_llm = copy(llm)
    configured_model = run.run_config.get("model")
    if configured_model:
        run_llm.model = str(configured_model)
    configured_transport = run.run_config.get("transport")
    if configured_transport:
        run_llm.transport_name = resolve_transport(
            run_llm.base_url, str(configured_transport)
        )

    child_agent = Agent(
        run_llm,
        child_tools,
        child_session,
        SilentRenderer(),
        max_consecutive_invalid=3,
        permission_resolver=permission_resolver,
        cancellation_check=cancelled,
        usage_observer=observe_usage,
        allow_background_tasks=False,
        lifecycle=lifecycle,
        services=services,
        profile=AgentProfile(
            "durable", frozenset(tool.name for tool in child_tools),
            max_steps=record.step_budget,
        ),
        execution_journal=_DurableToolJournal(scheduler, run_id),
    )
    user_prompt = _user_prompt_for_run(scheduler, run_id)

    def run_durable() -> None:
        final_answer: str | None = None
        task_status = "failed"
        error = ""
        try:
            final_answer = child_agent.run(
                user_prompt, max_steps=record.step_budget
            )
            if (
                scheduler.store.is_cancel_requested(run_id)
                or control.is_cancelled(record.id)
            ):
                task_status = "cancelled"
                error = (
                    scheduler.store.get_run(run_id).cancel_reason
                    or control.cancellation_reason(record.id)
                    or "durable run cancelled"
                )
            elif final_answer is None:
                task_status = "failed"
                error = (
                    "autonomous Agent ended with session status="
                    f"{child_session.current_run_status()}"
                )
            else:
                task_status, error = "completed", ""
        except Exception as exc:
            final_answer = None
            task_status, error = (
                "failed",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            _commit_durable_run(
                scheduler=scheduler,
                control=control,
                run_id=run_id,
                task_id=record.id,
                session=child_session,
                final_answer=final_answer,
                task_status=task_status,
                error=error,
            )

    try:
        background_runtime.submit(
            record.id,
            run_durable,
            control,
            done_event="DURABLE_RUN_FINISHED",
            done_payload=run_id,
        )
    except Exception as exc:
        control.finish_task(
            record.id, status="failed", steps_used=0, error=str(exc)
        )
        scheduler.finish_run(run_id, status="failed", error=str(exc))
        raise

    return DurableLaunch(
        run_id=run_id,
        task_id=record.id,
        session=child_session,
        tool_names=tuple(tool.name for tool in child_tools),
    )
