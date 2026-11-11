"""Messages the user sends while a turn runs.

The session service owns the queue. Between model requests, after a batch of
tool results is recorded, the root agent takes the queued text messages and
adds them to this turn's context, so the next request already sees them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ...core.logger import get_logger
from ...domain.model.session import USER_INTERJECTION_SOURCE, Session
from .run_events import AgentRunEvents

logger = get_logger(__name__)


class InterjectionInbox(Protocol):
    def take_interjections(self) -> list[dict[str, Any]]: ...

    def interjection_delivered(self, command_id: str, message_index: int) -> None: ...

    def interjection_rejected(self, command_id: str, reason: str) -> None: ...


def deliver_interjections(
    session: Session,
    inbox: InterjectionInbox | None,
    run_events: AgentRunEvents,
    checkpoint: Callable[[], None],
) -> int:
    """Add waiting user messages to the running root turn. Returns how many."""
    if inbox is None or session.agent_task_id is not None:
        return 0
    try:
        items = inbox.take_interjections()
    except Exception:
        # The messages stay queued and start their own turn later.
        logger.warning("could not take queued messages for the running turn", exc_info=True)
        return 0
    run = session.active_run()
    run_id = run.run_id if run is not None else ""
    delivered = 0
    for item in items:
        command_id = str(item.get("command_id", ""))
        prompt = str(item.get("prompt", ""))
        decision = run_events.emit("user_prompt_submit", {"prompt": prompt, "attachments": []})
        if decision is not None and decision.decision == "deny":
            inbox.interjection_rejected(
                command_id, decision.reason or "A lifecycle hook rejected the message",
            )
            continue
        message_index = len(session.message_records)
        session.append_user_message(prompt, source=USER_INTERJECTION_SOURCE)
        run_events.record(
            "user_input",
            {"session_run_id": run_id, "prompt": prompt, "attachment_ids": [], "interjection": True},
            event_key=f"user:{run_id}:{command_id}" if run_id else "",
        )
        if decision is not None and decision.additional_context:
            session.append_message({
                "role": "user",
                "content": (
                    "<hook-additional-context>\n"
                    f"{decision.additional_context}\n"
                    "</hook-additional-context>"
                ),
            }, source="system_feedback")
        inbox.interjection_delivered(command_id, message_index)
        delivered += 1
    if items:
        checkpoint()
    return delivered


__all__ = ["InterjectionInbox", "deliver_interjections"]
