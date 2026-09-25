"""Narrow runtime capabilities provided to a tool invocation.

This module is deliberately the only adapter that knows how a :class:`Session`
is assembled.  Tools receive the resulting domain capabilities, never a
Session, Run store, or RuntimeServices container.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..execution import ExecutionBackend, LocalExecutionBackend
from ..permission.types import AuthorizationChange
from ..processes import RuntimeResources
from ..tasks import TaskService
from ..tools.capabilities import (
    BackgroundTaskOperations,
    DelegationOperations,
    RunScope,
    ToolCapabilities,
)

if TYPE_CHECKING:
    from .services import RuntimeServices
    from ..domain.session import Session


@dataclass(frozen=True)
class CapabilityAssembly:
    """The composition result for one executor/runtime boundary.

    ``backend`` is intentionally kept beside, rather than inside, the
    long-lived capability view.  Tool code receives only the per-invocation
    :class:`AuthorizedExecution` wrapper built from this private backend.
    ``workspace_dir`` and ``cwd_provider`` are the environment facts the
    executor records and consults; it does not derive them from a Session.
    """

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
) -> CapabilityAssembly:
    """Build explicit capabilities at the application boundary.

    ``ToolExecutor`` receives this result.  It does not interpret a Session
    or ``RuntimeServices`` container itself.
    """

    workspace = (
        workspace_dir or getattr(session, "workspace_dir", None) or Path.cwd()
    ).resolve()
    cwd = cwd_provider or (
        session.get_cwd if session is not None else lambda: workspace
    )
    resources = runtime_resources
    if resources is None and session is not None:
        resources = RuntimeResources.for_session(session.session_id)
    backend = execution_backend or LocalExecutionBackend(workspace, cwd)
    if session is None:
        scope = RunScope("")
        return CapabilityAssembly(
            ToolCapabilities(scope), resources, backend, workspace, cwd
        )

    active_run = session.active_run()
    scope = RunScope(
        session_id=session.session_id,
        run_id=active_run.run_id if active_run is not None else "",
        root_turn_id=session.agent_root_turn_id,
        agent_task_id=session.agent_task_id,
    )
    return CapabilityAssembly(
        ToolCapabilities(
            scope=scope,
            set_cwd=lambda path: session.set_cwd(Path(path.value)),
            plan_manager=session.plan_manager,
            tasks=TaskService.for_session(session, services, resources),
            background_tasks=BackgroundTaskOperations(session.register_background_task),
            delegation=DelegationOperations(
                session.control_plane,
                services.agent_background if services is not None else None,
                execution_journal_factory,
                authorization_commit_factory,
            ),
            durable_store=services.durable_store if services is not None else None,
            autonomy_scheduler=services.autonomy_scheduler if services is not None else None,
            loop_registry=services.loop_registry if services is not None else None,
        ),
        resources,
        backend,
        workspace,
        cwd,
    )
