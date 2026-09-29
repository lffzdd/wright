"""Process-local runtime services shared by an Agent tree.

These are live handles (threads, queues, DB connections), not session state.
They travel with ToolRuntime and are intentionally absent from checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...infrastructure.persistence.autonomy_store import AutonomyStore
    from ..agent import AgentBackgroundRuntime
    from ..scheduling.scheduler import JobScheduler
    from ..session.loops import SessionLoopRegistry


@dataclass
class RuntimeServices:
    agent_background: AgentBackgroundRuntime | None = None
    # Shared task database: job definitions, runs, commands, interactions, tool logs.
    durable_store: AutonomyStore | None = None
    job_scheduler: JobScheduler | None = None
    loop_registry: SessionLoopRegistry | None = None
