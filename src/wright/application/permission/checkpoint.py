"""Explicit checkpoint participant bound to its owning session by composition."""
from dataclasses import dataclass
from typing import Any, Protocol


class CheckpointParticipant(Protocol):
    def save(self) -> None: ...


@dataclass(frozen=True)
class SessionCheckpoint:
    repository: Any
    session: Any

    def save(self) -> None:
        self.repository.save(self.session)
