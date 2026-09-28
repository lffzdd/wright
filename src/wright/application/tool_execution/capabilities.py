"""Narrow runtime capabilities provided to a tool invocation.

This module is the composition boundary. Tools receive operation objects,
never a Session, a store, a scheduler, or a process registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...domain.policy.permission.types import AuthorizationChange
from ...infrastructure.runtime import (
    ExecutionBackend,
    LocalExecutionBackend,
)
from ...infrastructure.tools.capabilities import (
    AgentOperations,
    AutonomyOperations,
    CommandOperations,
    DelegationOperations,
    RunScope,
    ToolCapabilities,
)
from ..agent.operations import AgentExecution
from ..autonomy.service import AutonomyService
from ..execution.identity import bind_identity
from ..session.live_resources import RuntimeResources

if TYPE_CHECKING:
    from ...domain.model.session import Session
    from ..composition.services import RuntimeServices


@dataclass(frozen=True)
class CapabilityAssembly:
    """The composition result for one executor/runtime boundary."""

    capabilities: ToolCapabilities
    runtime_resources: RuntimeResources | None
    backend: ExecutionBackend
    workspace_dir: Path
    cwd_provider: Callable[[], Path]


def assemble_tool_capabilities(
    session: Session | None,
    services: RuntimeServices | None,
    runtime_resources: RuntimeResources | None,
    *,
    workspace_dir: Path | None = None,
    cwd_provider: Callable[[], Path] | None = None,
    execution_backend: ExecutionBackend | None = None,
    execution_journal_factory: Callable[[str, str], Any] | None = None,
    authorization_commit_factory: (
        Callable[[Session], Callable[[AuthorizationChange], None]] | None
    ) = None,
    expose_autonomy: bool = False,
) -> CapabilityAssembly:
    """Build explicit capabilities at the application boundary."""

    workspace = (
        workspace_dir or getattr(session, "workspace_dir", None) or Path.cwd()
    ).resolve()
    cwd = cwd_provider or (
        session.get_cwd if session is not None else lambda: workspace
    )
    backend = execution_backend or LocalExecutionBackend(workspace, cwd)
    if session is None:
        return CapabilityAssembly(
            ToolCapabilities(RunScope("")),
            runtime_resources,
            backend,
            workspace,
            cwd,
        )

    active_run = session.active_run()
    scope = RunScope(
        session_id=session.session_id,
        run_id=active_run.run_id if active_run is not None else "",
        root_turn_id=session.agent_root_turn_id,
        agent_task_id=session.agent_task_id,
    )
    identity = (
        runtime_resources.identity
        if runtime_resources is not None and runtime_resources.identity is not None
        else bind_identity(
            session,
            services.durable_store if services is not None else None,
        )
    )
    commands = _command_operations(runtime_resources)
    agent_execution = AgentExecution(session.control_plane, identity)
    agents = AgentOperations(
        get=agent_execution.get,
        wait=agent_execution.wait,
        cancel=agent_execution.cancel,
        tree=lambda root: session.control_plane.tree_summary(root),
        limits=lambda: session.control_plane.config.to_dict(),
    )
    autonomy = None
    if (
        expose_autonomy
        and services is not None
        and services.durable_store is not None
    ):
        service = AutonomyService(
            services.durable_store, identity, services.autonomy_scheduler
        )
        autonomy = AutonomyOperations(
            create_schedule=service.create_schedule,
            get_schedule=service.get_schedule,
            list_schedules=service.list_schedules,
            pause_schedule=service.pause_schedule,
            resume_schedule=service.resume_schedule,
            cancel_schedule=service.cancel_schedule,
            list_runs=service.list_runs,
            get_run=service.get_run,
            wait_run=service.wait_run,
            cancel_run=service.cancel_run,
        )
    return CapabilityAssembly(
        ToolCapabilities(
            scope=scope,
            set_cwd=lambda path: session.set_cwd(Path(path.value)),
            plan_manager=session.plan_manager,
            commands=commands,
            agents=agents,
            autonomy=autonomy,
            delegation=_delegation(
                session.control_plane,
                services,
                execution_journal_factory,
                authorization_commit_factory,
            ),
            loop_registry=services.loop_registry if services is not None else None,
        ),
        runtime_resources,
        backend,
        workspace,
        cwd,
    )


def _command_operations(resources: RuntimeResources | None) -> CommandOperations | None:
    execution = resources.commands if resources is not None else None
    if execution is None:
        return None
    return CommandOperations(
        execute=execution.execute,
        get=execution.get,
        wait=execution.wait,
        terminate=execution.terminate,
        list=execution.list,
        notice=execution.notice,
    )


def _delegation(control, services, journal_factory, authorization_factory) -> DelegationOperations:
    background = services.agent_background if services is not None else None

    def begin_task(**kwargs):
        return control.begin_task(**kwargs)

    def finish_task(*args, **kwargs):
        return control.finish_task(*args, **kwargs)

    def bind_child_session(task_id: str, child_session_id: str) -> None:
        control.bind_child_session(task_id, child_session_id)

    def add_usage(task_id: str, prompt: int, completion: int, total: int) -> None:
        control.add_usage(task_id, prompt, completion, total)

    def request_cancel(task_id: str, reason: str) -> None:
        control.request_cancel(task_id, reason)

    def share_control_plane(child) -> None:
        child.control_plane = control

    def submit_background(task_id: str, run) -> None:
        if background is None:
            raise RuntimeError("background agent runtime is not configured")
        background.submit(task_id, run, control)

    return DelegationOperations(
        begin_task=begin_task,
        finish_task=finish_task,
        bind_child_session=bind_child_session,
        add_usage=add_usage,
        request_cancel=request_cancel,
        is_cancelled=lambda task_id: control.is_cancelled(task_id),
        cancellation_reason=lambda task_id: control.cancellation_reason(task_id),
        share_control_plane=share_control_plane,
        limits=lambda: control.config.to_dict(),
        tree=lambda root_turn_id: control.tree_summary(root_turn_id),
        submit_background=submit_background if background is not None else None,
        execution_journal_factory=journal_factory,
        authorization_commit_factory=authorization_factory,
    )


__all__ = ["CapabilityAssembly", "assemble_tool_capabilities"]
