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

from .execution import ExecutionBackend, ExecutionPath, LocalExecutionBackend
from .permission.types import AuthorizationChange
from .processes import RuntimeResources
from .tasks import TaskService

if TYPE_CHECKING:
    from .app.services import RuntimeServices
    from .autonomy.scheduler import AutonomyScheduler
    from .autonomy.store import AutonomyStore
    from .domain.coordination import AgentControlPlane
    from .domain.session import BackgroundTask, Session
    from .engine.agent_background import AgentBackgroundRuntime
    from .engine.looping import SessionLoopRegistry
    from .planning import PlanManager


@dataclass(frozen=True)
class RunScope:
    """Stable identity of the run currently allowed to invoke a tool."""

    session_id: str
    run_id: str = ""
    root_turn_id: str = ""
    agent_task_id: str | None = None


@dataclass(frozen=True)
class BackgroundTaskOperations:
    """The only tool-facing mutation route for persisted shell task records."""

    register: Callable[[BackgroundTask], None]


@dataclass(frozen=True)
class DelegationOperations:
    """Bounded access to the task-tree owner and its background runner."""

    control: AgentControlPlane
    agent_background: AgentBackgroundRuntime | None = None
    # Optional durable-run journal factory.  This is deliberately a single
    # operation, not a store/service container: a child receives a journal
    # scoped to its task identity and cannot mutate the parent Run directly.
    execution_journal_factory: Callable[[str, str], Any] | None = None
    authorization_commit_factory: (
        Callable[[Session], Callable[[AuthorizationChange], None]] | None
    ) = None


@dataclass(frozen=True)
class CapabilityAssembly:
    """The composition result for one executor/runtime boundary.

    ``backend`` is intentionally kept beside, rather than inside, the
    long-lived capability view.  Tool code receives only the per-invocation
    :class:`AuthorizedExecution` wrapper built from this private backend.
    """

    capabilities: ToolCapabilities
    runtime_resources: RuntimeResources | None
    backend: ExecutionBackend


@dataclass(frozen=True)
class ToolCapabilities:
    """Explicit capability set for one executor/run assembly.

    Each member is a domain owner or a narrow operation.  This is not a
    renamed service bag: absent capabilities are unavailable to the tool.
    """

    scope: RunScope
    # Cwd mutation is deliberately opt-in. A planning or scheduling tool must
    # not receive it merely because another tool in the same Run needs it.
    set_cwd: Callable[[ExecutionPath], None] | None = None
    plan_manager: PlanManager | None = None
    tasks: TaskService | None = None
    background_tasks: BackgroundTaskOperations | None = None
    delegation: DelegationOperations | None = None
    durable_store: AutonomyStore | None = None
    autonomy_scheduler: AutonomyScheduler | None = None
    loop_registry: SessionLoopRegistry | None = None

    def restricted(self, required: frozenset[str]) -> ToolCapabilities:
        """Return a call-local view containing only declared operations."""
        # Scope is the foundational identity; all other owners are opt-in.
        # Execution is intentionally absent here: it is assembled per call by
        # ToolExecutor and exposed only as ToolRuntime.execution.
        return ToolCapabilities(
            scope=self.scope,
            set_cwd=self.set_cwd if "cwd" in required else None,
            plan_manager=self.plan_manager if "plan" in required else None,
            tasks=self.tasks if "tasks" in required else None,
            background_tasks=self.background_tasks if "background" in required else None,
            delegation=self.delegation if "delegation" in required else None,
            durable_store=self.durable_store if "durable" in required else None,
            autonomy_scheduler=self.autonomy_scheduler if "autonomy" in required else None,
            loop_registry=self.loop_registry if "loop" in required else None,
        )


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
    """Build explicit capabilities once, at the application/executor boundary."""

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
        return CapabilityAssembly(ToolCapabilities(scope), resources, backend)

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
    )
