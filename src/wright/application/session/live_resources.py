"""Process-local resources for one session.

The host creates this object and passes it into the agent and the tool
assembly. Nothing looks it up by session id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..command.execution import CommandExecution
from ..execution.identity import ExecutionIdentity
from .response_projection import ResponseProjection


@dataclass
class RuntimeResources:
    session_id: str
    commands: CommandExecution | None = None
    identity: ExecutionIdentity | None = None
    responses: ResponseProjection = field(default_factory=ResponseProjection)

    def close(self) -> tuple[str, ...]:
        terminated = self.commands.close() if self.commands is not None else ()
        self.responses.clear()
        return terminated


__all__ = ["RuntimeResources"]
