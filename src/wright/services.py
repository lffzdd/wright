"""Process-local runtime services shared by an Agent tree.

These are live handles (threads, queues, DB connections), not session state.
They travel with ToolRuntime and are intentionally absent from checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_background import AgentBackgroundRuntime
    from .autonomy.scheduler import AutonomyScheduler
    from .autonomy.store import AutonomyStore
    from .looping import SessionLoopRegistry


@dataclass(frozen=True)
class RuntimeServices:
    agent_background: "AgentBackgroundRuntime | None" = None
    durable_store: "AutonomyStore | None" = None
    autonomy_scheduler: "AutonomyScheduler | None" = None
    loop_registry: "SessionLoopRegistry | None" = None
