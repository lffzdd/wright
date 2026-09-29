"""Bounded operations exposed to one tool call.

These objects are the operations a tool may invoke. They are not the session,
the process registry, the shared task store, or the job scheduler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .ports import LoopOperations


@dataclass(frozen=True)
class RunScope:
    """Stable identity of the run currently allowed to invoke a tool."""

    session_id: str
    run_id: str = ""
    root_turn_id: str = ""
    agent_task_id: str | None = None


@dataclass(frozen=True)
class CommandOperations:
    execute: Callable[..., Any]
    get: Callable[..., dict]
    wait: Callable[..., dict]
    terminate: Callable[..., dict]
    list: Callable[..., dict]
    notice: Callable[[str], dict | None]


@dataclass(frozen=True)
class AgentOperations:
    get: Callable[..., Any]
    wait: Callable[..., Any]
    cancel: Callable[..., tuple]
    tree: Callable[..., list]
    limits: Callable[[], dict]


@dataclass(frozen=True)
class SchedulingOperations:
    """Schedule-shaped callables. The service underneath speaks in jobs."""

    create_job: Callable[..., Any]
    get_job: Callable[..., Any]
    list_jobs: Callable[..., Any]
    pause_job: Callable[..., Any]
    resume_job: Callable[..., Any]
    cancel_job: Callable[..., Any]
    update_job: Callable[..., Any]
    delete_job: Callable[..., Any]
    list_runs: Callable[..., Any]
    get_run: Callable[..., Any]
    wait_run: Callable[..., Any]
    cancel_run: Callable[..., Any]


@dataclass(frozen=True)
class DelegationOperations:
    """Closures over the control plane. The plane itself is not an attribute."""

    begin_task: Callable[..., Any]
    finish_task: Callable[..., Any]
    bind_child_session: Callable[..., None]
    add_usage: Callable[..., None]
    request_cancel: Callable[..., None]
    is_cancelled: Callable[[str], bool]
    cancellation_reason: Callable[[str], str]
    share_control_plane: Callable[[Any], None]
    limits: Callable[[], dict]
    tree: Callable[..., list]
    submit_background: Callable[..., None] | None = None
    execution_journal_factory: Callable[[str, str], Any] | None = None
    authorization_commit_factory: Callable[..., Any] | None = None


@dataclass(frozen=True)
class ToolCapabilities:
    """Explicit capability set for one executor/run assembly."""

    scope: RunScope
    set_cwd: Callable[..., None] | None = None
    plan_manager: Any = None
    commands: CommandOperations | None = None
    agents: AgentOperations | None = None
    scheduling: SchedulingOperations | None = None
    delegation: DelegationOperations | None = None
    loop_registry: LoopOperations | None = None

    def restricted(self, required: frozenset[str]) -> ToolCapabilities:
        """Return a call-local view containing only declared operations."""
        return ToolCapabilities(
            scope=self.scope,
            set_cwd=self.set_cwd if "cwd" in required else None,
            plan_manager=self.plan_manager if "plan" in required else None,
            commands=self.commands if "commands" in required else None,
            agents=self.agents if "agents" in required else None,
            scheduling=self.scheduling if "scheduling" in required else None,
            delegation=self.delegation if "delegation" in required else None,
            loop_registry=self.loop_registry if "loop" in required else None,
        )


__all__ = [
    "AgentOperations",
    "CommandOperations",
    "DelegationOperations",
    "RunScope",
    "SchedulingOperations",
    "ToolCapabilities",
]
