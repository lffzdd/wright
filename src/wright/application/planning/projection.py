"""Display-only wait details from the current plan and live interactions."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ...domain.model.session import Session
    from ..session.events import UiEventEnvelope
    from ..session.publisher import EventPublisher


class PendingInteractions(Protocol):
    def snapshot(self) -> list[dict[str, Any]]: ...


def project_plan(
    plan: dict,
    pending: list[dict[str, Any]],
    *,
    owner_task_id: str = "",
    owner_session_id: str = "",
    observed_at: float | None = None,
) -> dict:
    result = deepcopy(plan)
    now = time.time() if observed_at is None else observed_at
    result["observed_at"] = now
    # Never reuse transient wait reasons from a checkpoint or tool result.
    result.pop("wait_reason", None)
    for step in result.get("steps", []):
        started, ended = step["started_at"], step["ended_at"]
        step.pop("elapsed_ms", None)
        if started is not None:
            step["elapsed_ms"] = max(
                0, int(((ended if ended is not None else now) - started) * 1000)
            )
        step.pop("wait_reason", None)
        if step.get("status") == "blocked":
            step["wait_reason"] = {
                "kind": "blocked",
                "detail": step.get("note", ""),
                "request_ids": [],
            }
    active = next(
        (
            step
            for step in result.get("steps", [])
            if step.get("status") == "in_progress"
        ),
        None,
    )
    unrelated = []
    unrelated_kinds = set()
    for kind in ("permission", "ask_user"):
        matching = [item for item in pending if item.get("kind") == kind]
        owned = [
            item for item in matching if _owner(item, owner_session_id) == owner_task_id
        ]
        other = [
            item for item in matching if _owner(item, owner_session_id) != owner_task_id
        ]
        if active is not None and owned:
            active.setdefault("wait_reason", {"kind": kind, "request_ids": []})[
                "request_ids"
            ].extend(str(item["request_id"]) for item in owned)
            if active["wait_reason"]["kind"] != kind:
                active["wait_reason"]["kind"] = "user_input"
        else:
            other += owned
        unrelated.extend(str(item["request_id"]) for item in other)
        if other:
            unrelated_kinds.add(kind)
    if unrelated:
        result["wait_reason"] = {
            "kind": next(iter(unrelated_kinds))
            if len(unrelated_kinds) == 1
            else "user_input",
            "request_ids": unrelated,
        }
    return result


def _owner(item: dict[str, Any], session_id: str) -> str:
    task_id = str(item.get("agent_task_id") or "")
    principal = str(item.get("principal") or "")
    if not task_id and principal and principal != session_id:
        return principal
    if int(item.get("agent_depth") or 0) > 0 and not task_id:
        return "<unassigned-child>"
    return task_id


def bind_plan_events(
    publisher: EventPublisher, session: Session, interactions: PendingInteractions
) -> str:
    """Publish authoritative projections after plan tools and mailbox changes."""

    def publish_plan_view(event: UiEventEnvelope) -> None:
        root_event = not event.payload.get("agent_task_id") and not event.payload.get(
            "agent_depth"
        )
        is_plan_tool = (
            event.type == "tool.finished"
            and root_event
            and event.payload.get("name")
            in {"create_plan", "update_plan", "replan", "get_plan"}
        )
        if (
            event.type in {"interaction.requested", "interaction.resolved"}
            or is_plan_tool
            or (
                root_event
                and event.type in {"turn.completed", "turn.failed", "turn.cancelled"}
            )
            or (
                root_event
                and event.type == "session.status_changed"
                and event.payload.get("status") == "model_turn_started"
            )
        ):
            publisher.publish(
                "plan.updated",
                {
                    "plan": project_plan(
                        session.plan_manager.snapshot(),
                        interactions.snapshot(),
                        owner_task_id=session.agent_task_id or "",
                        owner_session_id=session.session_id,
                    ),
                },
            )

    return publisher.add_listener(publish_plan_view)
