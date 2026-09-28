"""Process-local session resources: shell handles and the streaming snapshot.

Shell handles live in infrastructure and are shared with tools. The response
snapshot is only for interfaces that render a live turn.
"""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field

from ...infrastructure.runtime.process_registry import ProcessRegistry, SessionProcesses
from .response_projection import ResponseProjection


@dataclass
class RuntimeResources:
    session_id: str
    process_registry: ProcessRegistry = field(init=False)
    responses: ResponseProjection = field(default_factory=ResponseProjection)

    def __post_init__(self) -> None:
        shared = SessionProcesses.for_session(self.session_id)
        assert shared is not None
        self.process_registry = shared.process_registry
        with _SESSIONS_LOCK:
            _SESSIONS[self.session_id] = self

    @classmethod
    def for_session(
        cls, session_id: str, *, create: bool = True
    ) -> RuntimeResources | None:
        with _SESSIONS_LOCK:
            current = _SESSIONS.get(session_id)
            if current is not None or not create:
                return current
            return cls(session_id)

    def close(self) -> tuple[str, ...]:
        terminated = self.process_registry.terminate_all()
        self.responses.clear()
        with _SESSIONS_LOCK:
            if _SESSIONS.get(self.session_id) is self:
                _SESSIONS.pop(self.session_id, None)
        return terminated


_SESSIONS: weakref.WeakValueDictionary[str, RuntimeResources] = weakref.WeakValueDictionary()
_SESSIONS_LOCK = threading.RLock()
