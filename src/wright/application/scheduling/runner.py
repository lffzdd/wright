"""Isolated background execution for one persisted job run.

The REPL thread constructs the session and submits a worker. The scheduler
thread still only writes through its dispatch port and enqueues
``DURABLE_RUN_DUE``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from typing import Any

from ...core.logger import get_logger
from ...domain.model.agent import AgentProfile
from ...domain.model.agent.control import AgentControlError, AgentControlPlane
from ...domain.model.llm import UsageRecord
from ...domain.model.scheduling import JobRun
from ...domain.model.session import Session
from ...domain.policy import PermissionResolver, PermissionSettings
from ...infrastructure.llm.llm import LLMClient, resolve_transport
from ...infrastructure.tools.base import Tool
from ..agent import (
    Agent,
    AgentBackgroundRuntime,
    assemble_agent_components,
    build_agent_tools,
    ensure_system_prompt,
    prepare_model_tools,
)
from ..command.execution import CommandExecution
from ..composition.roles import tools_for_role
from ..composition.services import RuntimeServices
from ..execution.directory import (
    DirectoryLease,
    bind_directory_work,
    reset_directory_work,
)
from ..execution.identity import bind_identity
from ..session.live_resources import RuntimeResources
from ..session.publisher import open_session_events
from ..tool_execution.capabilities import assemble_tool_capabilities
from .contracts import RunExecution
from .scheduler import JobScheduler

logger = get_logger(__name__)


class _RunJournal:
    """Appends tool facts for one job run. It does not see the job catalog."""

    def __init__(
        self,
        execution: RunExecution,
        run_id: str,
        *,
        agent_task_id: str = "",
        parent_agent_task_id: str = "",
    ) -> None:
        self._execution = execution
        self.run_id = run_id
        self.agent_task_id = agent_task_id
        self.parent_agent_task_id = parent_agent_task_id
        self.session_run_id = ""
        self._call_keys: dict[str, str] = {}

    def bind_run(self, session_run_id: str) -> None:
        self.session_run_id = str(session_run_id)[:300]
        self.record_event(
            "run_bound",
            {
                "session_run_id": self.session_run_id,
                "agent_task_id": self.agent_task_id,
                "parent_agent_task_id": self.parent_agent_task_id,
            },
            event_key=f"run-bound:{self.agent_task_id}:{self.session_run_id}",
        )

    def child(self, agent_task_id: str, parent_agent_task_id: str = "") -> _RunJournal:
        return _RunJournal(
            self._execution,
            self.run_id,
            agent_task_id=agent_task_id,
            parent_agent_task_id=parent_agent_task_id or self.agent_task_id,
        )

    def record_intent(self, **value: Any) -> None:
        provider_call_id = str(value.get("call_id", ""))
        execution_call_id = self._execution.record_tool_intent(
            run_id=self.run_id,
            agent_task_id=self.agent_task_id,
            session_run_id=self.session_run_id,
            parent_agent_task_id=self.parent_agent_task_id,
            **value,
        )
        self._call_keys[provider_call_id] = execution_call_id
        self.record_event(
            "tool_intent",
            {"call_id": provider_call_id, "execution_call_id": execution_call_id,
             "tool_name": value.get("tool_name", ""), "agent_task_id": self.agent_task_id,
             "session_run_id": self.session_run_id},
            event_key=f"intent:{self.agent_task_id}:{execution_call_id}",
        )

    def mark_started(self, call_id: str) -> None:
        execution_call_id = self._call_keys.get(call_id, call_id)
        self._execution.mark_tool_started(self.run_id, execution_call_id)
        self.record_event(
            "tool_started",
            {"call_id": call_id, "execution_call_id": execution_call_id,
             "agent_task_id": self.agent_task_id},
            event_key=f"started:{self.agent_task_id}:{execution_call_id}",
        )

    def record_result(self, call_id: str, result: dict[str, Any], *, status: str) -> None:
        execution_call_id = self._call_keys.get(call_id, call_id)
        self._execution.record_tool_result(self.run_id, execution_call_id, result, status=status)
        self.record_event(
            "tool_result",
            {"call_id": call_id, "execution_call_id": execution_call_id,
             "status": status, "agent_task_id": self.agent_task_id, "result": result},
            event_key=f"result:{self.agent_task_id}:{execution_call_id}",
        )

    def record_event(
        self, event_type: str, payload: dict[str, Any] | None = None, *, event_key: str = ""
    ) -> str:
        return self._execution.record_run_event(
            self.run_id,
            event_type,
            {
                **(payload or {}),
                "agent_task_id": self.agent_task_id,
                "parent_agent_task_id": self.parent_agent_task_id,
            },
            event_key=event_key,
        )

# Isolated workers start at depth=1 and may spawn one leaf helper.
DURABLE_MAX_DEPTH = 2


_UNATTENDED_DENY_NOTE = (
    "durable run is unattended and cannot prompt for confirmation"
)


@dataclass(frozen=True)
class JobLaunch:
    run_id: str
    task_id: str
    session: Session
    tool_names: tuple[str, ...]


def _durable_base_tools(base_tools: Sequence[Tool]) -> list[Tool]:
    return tools_for_role(base_tools, "durable")


def _durable_root_turn_id(run_id: str) -> str:
    return f"durable:{run_id}"


def _user_prompt_for_run(scheduler: JobScheduler, run_id: str) -> str:
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
    # The policy is the single source of truth.  With no approval adapter,
    # every policy-level ask fails closed without an interactive fallback.
    return PermissionResolver(settings=settings)


def _commit_job_run(
    *,
    scheduler: JobScheduler,
    execution: RunExecution,
    control: AgentControlPlane,
    run_id: str,
    task_id: str,
    session: Session,
    final_answer: str | None,
    task_status: str,
    error: str,
) -> JobRun:
    current = execution.get_run(run_id)
    if current.terminal:
        finished = current
    else:
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
                error = f"autonomous Agent ended with run status={session.current_run_status()}"
        finished = scheduler.finish_run(
            run_id, status=status, result=final_answer or "", error=error,
        )
    control.finish_task(
        task_id,
        status=finished.status if finished.terminal else "failed",
        steps_used=session.step_count,
        result=finished.result if finished.terminal else "",
        error=finished.error,
    )
    return finished


def launch_job_run(
    *,
    run_id: str,
    execution: RunExecution,
    root_session: Session | None = None,
    workspace_dir=None,
    control_plane: AgentControlPlane | None = None,
    scheduler: JobScheduler,
    llm: LLMClient,
    base_tools: Sequence[Tool],
    permission_settings: PermissionSettings,
    background_runtime: AgentBackgroundRuntime,
    lifecycle: Any = None,
    services: RuntimeServices,
    max_steps: int = 50,
    max_depth: int = DURABLE_MAX_DEPTH,
    directory_coordinator: Any = None,
    directory_lease: DirectoryLease | None = None,
) -> JobLaunch | None:
    """Construct an isolated job-run session on the REPL thread and return.

    The worker runs in the shared background pool. Callers must not wait.
    A directory lease passed in stays held until that worker finishes.
    ``execution`` is only the run log for this launch, not the job catalog.
    """

    def release_directory() -> None:
        if directory_lease is not None:
            directory_lease.release()

    run = execution.get_run(run_id)
    if run.status != "dispatched":
        release_directory()
        return None
    configured_steps = run.run_config.get("max_steps")
    if configured_steps is not None:
        max_steps = int(configured_steps)
    execution.start_run(run_id, owner_id=scheduler.host_id)
    root_turn_id = _durable_root_turn_id(run_id)
    execution.set_run_root_turn(run_id, root_turn_id)

    if root_session is None and workspace_dir is None:
        raise ValueError("durable run requires workspace_dir when no source Session exists")
    control = control_plane or (
        root_session.control_plane if root_session is not None else AgentControlPlane()
    )
    prompt = run.prompt.strip() or run.job_name or "durable task"
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
        release_directory()
        raise

    if record.status != "running":
        scheduler.finish_run(
            run_id,
            status="failed",
            error=record.error or "control plane did not start durable task",
        )
        release_directory()
        return None

    permission_resolver = _fail_closed_resolver(permission_settings)

    def authorization_commit_factory(target_session: Session):
        def commit_authorization(change) -> None:
            from ..tool_execution.commit import commit_authorization as commit_change

            # Durable runs have no session checkpoint. The change stays on
            # target_session, which is the child when a child is committing.
            commit_change(change, session=target_session)

        return commit_authorization

    root_journal = _RunJournal(
        execution, run_id, agent_task_id=record.id
    )

    def child_journal_factory(agent_task_id: str, parent_agent_task_id: str):
        return root_journal.child(agent_task_id, parent_agent_task_id)

    child_tools = build_agent_tools(
        llm,
        _durable_base_tools(base_tools),
        depth=1,
        max_depth=min(max_depth, control.config.max_depth),
        render_subagents=False,
        permission_resolver=permission_resolver,
        authorization_commit_factory=authorization_commit_factory,
        enable_scheduling=False,
    )
    child_session = Session.create(
        initial_goal=prompt,
        workspace_dir=(root_session.workspace_dir if root_session is not None else workspace_dir),
        max_steps=record.step_budget,
        additional_working_directories=(
            list(root_session.working_directories_snapshot())
            if root_session is not None else None
        ),
    )
    child_session.control_plane = control
    child_session.agent_task_id = record.id
    child_session.agent_root_turn_id = root_turn_id
    control.bind_child_session(record.id, child_session.session_id)

    def cancelled() -> bool:
        if execution.is_cancel_requested(run_id):
            reason = (
                execution.get_run(run_id).cancel_reason
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

    durable_commands = CommandExecution(
        child_session,
        bind_identity(child_session),
        allow_background=False,
    )
    child_assembly = assemble_tool_capabilities(
        child_session,
        None,
        RuntimeResources(child_session.session_id, commands=durable_commands),
        execution_journal_factory=child_journal_factory,
        authorization_commit_factory=authorization_commit_factory,
    )
    durable_profile = AgentProfile(
        "durable", frozenset(tool.name for tool in child_tools),
        max_steps=record.step_budget,
        allow_delegation=max_depth > 1,
    )
    durable_prepared = prepare_model_tools(child_session, child_tools, durable_profile)
    ensure_system_prompt(child_session, durable_prepared, None)
    child_events = open_session_events(child_session)
    child_components = assemble_agent_components(
        session_state=child_session,
        events=child_events,
        prepared=durable_prepared,
        assembly=child_assembly,
        permission_resolver=permission_resolver,
        cancellation_check=cancelled,
        allow_background_tasks=False,
        lifecycle=lifecycle,
        execution_journal=root_journal,
        authorization_commit=authorization_commit_factory(child_session),
    )
    child_agent = Agent(
        run_llm,
        child_session,
        child_events,
        durable_prepared,
        child_assembly,
        components=child_components,
        max_consecutive_invalid=3,
        usage_observer=observe_usage,
        lifecycle=lifecycle,
        services=None,
        execution_journal=root_journal,
        execution_journal_factory=child_journal_factory,
        authorization_commit=authorization_commit_factory(child_session),
        authorization_commit_factory=authorization_commit_factory,
    )
    user_prompt = _user_prompt_for_run(scheduler, run_id)

    def run_durable() -> None:
        final_answer: str | None = None
        task_status = "failed"
        error = ""
        token = None
        if directory_lease is not None and directory_coordinator is not None:
            token = bind_directory_work(directory_coordinator, directory_lease)
        try:
            final_answer = child_agent.run(
                user_prompt, max_steps=record.step_budget
            )
            if (
                execution.is_cancel_requested(run_id)
                or control.is_cancelled(record.id)
            ):
                task_status = "cancelled"
                error = (
                    execution.get_run(run_id).cancel_reason
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
            if token is not None:
                reset_directory_work(token)
            release_directory()
            durable_commands.close()
            finished = _commit_job_run(
                scheduler=scheduler,
                execution=execution,
                control=control,
                run_id=run_id,
                task_id=record.id,
                session=child_session,
                final_answer=final_answer,
                task_status=task_status,
                error=error,
            )
            try:
                root_journal.record_event(
                    "run_finished",
                    {
                        "status": finished.status,
                        "result": finished.result,
                        "error": finished.error,
                    },
                    event_key="run-finished",
                )
            except Exception:
                # The terminal SQLite Run row remains authoritative; a
                # notification history failure cannot cause a second run.
                logger.error("could not append durable run_finished history", exc_info=True)

    try:
        background_runtime.submit(
            record.id,
            run_durable,
            control,
            done_event="DURABLE_RUN_FINISHED",
            done_payload=run_id,
        )
    except Exception as exc:
        durable_commands.close()
        release_directory()
        control.finish_task(
            record.id, status="failed", steps_used=0, error=str(exc)
        )
        scheduler.finish_run(run_id, status="failed", error=str(exc))
        raise

    return JobLaunch(
        run_id=run_id,
        task_id=record.id,
        session=child_session,
        tool_names=tuple(tool.name for tool in child_tools),
    )
