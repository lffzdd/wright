"""Bounded capability values exposed to one Agent/tool runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...domain.policy.types import AuthorizationChange
from .ports import LoopOperations

if TYPE_CHECKING:
    from ...autonomy.scheduler import AutonomyScheduler
    from ...autonomy.store import AutonomyStore
    from ...domain.coordination import AgentControlPlane
    from ...domain.session import BackgroundTask, Session
    from ...application.agent_background import AgentBackgroundRuntime
    from ..runtime import ExecutionPath
    from ...planning import PlanManager
    from ...application.task_service import TaskService


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
    execution_journal_factory: Callable[[str, str], Any] | None = None
    authorization_commit_factory: (
        Callable[[Session], Callable[[AuthorizationChange], None]] | None
    ) = None


@dataclass(frozen=True)
class ToolCapabilities:
    """Explicit capability set for one executor/run assembly."""

    scope: RunScope
    set_cwd: Callable[[ExecutionPath], None] | None = None
    plan_manager: PlanManager | None = None
    tasks: TaskService | None = None
    background_tasks: BackgroundTaskOperations | None = None
    delegation: DelegationOperations | None = None
    durable_store: AutonomyStore | None = None
    autonomy_scheduler: AutonomyScheduler | None = None
    loop_registry: LoopOperations | None = None

    def restricted(self, required: frozenset[str]) -> ToolCapabilities:
        """Return a call-local view containing only declared operations."""

        return ToolCapabilities(
            scope=self.scope,
            set_cwd=self.set_cwd if "cwd" in required else None,
            plan_manager=self.plan_manager if "plan" in required else None,
            tasks=self.tasks if "tasks" in required else None,
            background_tasks=(
                self.background_tasks if "background" in required else None
            ),
            delegation=self.delegation if "delegation" in required else None,
            durable_store=self.durable_store if "durable" in required else None,
            autonomy_scheduler=(
                self.autonomy_scheduler if "autonomy" in required else None
            ),
            loop_registry=self.loop_registry if "loop" in required else None,
        )
