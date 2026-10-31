"""Lifecycle events and durable run history for one Agent."""

from __future__ import annotations

from ...core.logger import get_logger
from ...domain.model.session import Session

logger = get_logger(__name__)


class AgentRunEvents:
    def __init__(self, session_state: Session, *, lifecycle=None, journal=None) -> None:
        self.session_state = session_state
        self.lifecycle = lifecycle
        self.journal = journal

    def emit(self, event: str, payload: dict):
        if self.lifecycle is None:
            return None
        return self.lifecycle.emit(
            event,
            payload,
            agent_task_id=self.session_state.agent_task_id,
            root_turn_id=self.session_state.agent_root_turn_id,
        )

    def record(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        event_key: str = "",
    ) -> None:
        """Persist one durable history fact before the run advances.

        Interactive sessions have no journal.  Durable hosts provide the
        narrow journal object.  A history write failure is a run failure, not
        a reason to keep executing with an unverifiable state transition.
        """
        journal = self.journal
        recorder = getattr(journal, "record_event", None) if journal is not None else None
        if not callable(recorder):
            return
        try:
            recorder(event_type, payload or {}, event_key=event_key)
        except Exception as exc:
            logger.error("durable Run history write failed", exc_info=True)
            raise RuntimeError(
                f"durable Run history persistence failed for {event_type}"
            ) from exc

    def agent_start(
        self,
        prompt: str,
        *,
        resumed: bool = False,
        source: str = "user",
    ) -> None:
        event = (
            "subagent_start"
            if self.session_state.agent_task_id is not None
            else "agent_start"
        )
        self.emit(event, {
            "prompt": prompt,
            "resumed": resumed,
            "source": source,
            "max_steps": self.session_state.max_steps,
        })

    def agent_stop(
        self,
        status: str,
        *,
        final_answer: str | None = None,
        reason: str = "",
    ):
        event = (
            "subagent_stop"
            if self.session_state.agent_task_id is not None
            else "agent_stop"
        )
        return self.emit(event, {
            "status": status,
            "final_answer": final_answer or "",
            "reason": reason,
            "steps": self.session_state.step_count,
            "usage": {
                "prompt_tokens": self.session_state.total_usage.prompt_tokens,
                "completion_tokens": self.session_state.total_usage.completion_tokens,
                "total_tokens": self.session_state.total_usage.total_tokens,
            },
        })
