"""Memory console views: what the user sees and edits in the web console.

Edits here are the user's own, so the service records them with actor "user":
they can pin, confirm quarantined facts, and mark facts shareable, which the
agent cannot. Deletes require an explicit confirmation flag.
"""

from __future__ import annotations

from typing import Any

from ...domain.model.memory import GLOBAL_SPACE_ID, repo_space_id
from ..memory.dto import WriteRequest
from ..memory.service import MemoryService, MemoryServiceError


class MemoryViewError(ValueError):
    pass


def _require(service: MemoryService | None) -> MemoryService:
    if service is None:
        raise MemoryViewError("memory service is not available")
    return service


def visible_spaces(service: MemoryService, bound_project_id: str) -> list[str]:
    spaces = [GLOBAL_SPACE_ID]
    if bound_project_id:
        spaces.append(service.ensure_repo_space(bound_project_id).id)
    spaces.extend(item.id for item in service.list_spaces() if item.type == "personal")
    return spaces


def project_memory(service: MemoryService | None, *, bound_project_id: str) -> dict[str, Any]:
    service = _require(service)
    spaces = visible_spaces(service, bound_project_id)
    facts = service.list_facts(space_ids=spaces, statuses=["active", "quarantined", "superseded"])
    episodes = service.list_episodes(project_ids=[bound_project_id], limit=50) if bound_project_id else []
    commitments = service.list_commitments(space_ids=spaces)
    return {
        "project_id": bound_project_id,
        "current_space": repo_space_id(bound_project_id) if bound_project_id else GLOBAL_SPACE_ID,
        "spaces": [item.to_dict() for item in service.list_spaces() if item.id in spaces],
        "facts": [item.to_dict() for item in facts],
        "episodes": [
            {
                "id": item.id,
                "goal": item.goal,
                "status": item.status,
                "outcome": item.outcome[:600],
                "created_at": item.created_at,
                "links": {key: list(value) for key, value in item.links.items()},
                "compacted": item.compacted,
            }
            for item in episodes
        ],
        "commitments": [item.to_dict() for item in commitments],
        "changes": [item.to_dict() for item in service.changes(limit=50)],
    }


def create_fact(
    service: MemoryService | None,
    *,
    bound_project_id: str,
    content: str,
    kind: str,
    subject: str = "other",
    space: str = "",
    why: str = "",
    pin: bool = False,
) -> dict[str, Any]:
    service = _require(service)
    current = service.ensure_repo_space(bound_project_id).id if bound_project_id else GLOBAL_SPACE_ID
    try:
        space_id = service.resolve_space(space or "current", current=current, allow_create=True)
    except MemoryServiceError as exc:
        raise MemoryViewError(str(exc)) from exc
    outcome = service.write_fact(WriteRequest(
        content=content, kind=kind, space_id=space_id, subject=subject, why=why, pin=pin, actor="user", force_new=True,
    ))
    if not outcome.ok or outcome.fact is None:
        raise MemoryViewError(outcome.message or "fact was not created")
    return outcome.fact.to_dict()


def update_fact(service: MemoryService | None, fact_id: str, *, bound_project_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    service = _require(service)
    _require_visible(service, fact_id, bound_project_id)
    try:
        fact = service.update_fact(
            fact_id,
            actor="user",
            expected_revision=fields.get("expected_revision"),
            content=fields.get("content"),
            why=fields.get("why"),
            kind=fields.get("kind"),
            tier=fields.get("tier"),
            shareable=fields.get("shareable"),
        )
    except (MemoryServiceError, ValueError) as exc:
        raise MemoryViewError(str(exc)) from exc
    except Exception as exc:
        raise MemoryViewError(f"update failed: {exc}") from exc
    return fact.to_dict()


def confirm_fact(service: MemoryService | None, fact_id: str, *, bound_project_id: str) -> dict[str, Any]:
    service = _require(service)
    _require_visible(service, fact_id, bound_project_id)
    try:
        return service.confirm_fact(fact_id).to_dict()
    except MemoryServiceError as exc:
        raise MemoryViewError(str(exc)) from exc


def forget(service: MemoryService | None, item_id: str, *, bound_project_id: str, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise MemoryViewError("confirmation is required")
    service = _require(service)
    _require_visible(service, item_id, bound_project_id)
    try:
        removed = service.forget(item_id, actor="user", reason="deleted in the memory console")
    except MemoryServiceError as exc:
        raise MemoryViewError(str(exc)) from exc
    return {"id": item_id, "deleted": True, "removed": removed}


def _require_visible(service: MemoryService, item_id: str, bound_project_id: str) -> None:
    """The console of one workspace cannot change another project's records."""
    if item_id.startswith("ep-"):
        episode = service.get_episode(item_id)
        if episode is not None and episode.project_id != bound_project_id:
            raise MemoryViewError("episode belongs to another project")
        return
    space_id = ""
    if item_id.startswith("f-"):
        fact = service.get_fact(item_id)
        space_id = fact.space_id if fact is not None else ""
    elif item_id.startswith("c-"):
        found = [item for item in service.list_commitments() if item.id == item_id]
        space_id = found[0].space_id if found else ""
    if space_id.startswith("repo:") and space_id != (repo_space_id(bound_project_id) if bound_project_id else ""):
        raise MemoryViewError("this belongs to another project")


def update_commitment(service: MemoryService | None, commitment_id: str, *, bound_project_id: str, status: str) -> dict[str, Any]:
    service = _require(service)
    _require_visible(service, commitment_id, bound_project_id)
    try:
        return service.update_commitment(commitment_id, actor="user", status=status).to_dict()
    except MemoryServiceError as exc:
        raise MemoryViewError(str(exc)) from exc


def revert_change(service: MemoryService | None, change_id: int) -> dict[str, Any]:
    service = _require(service)
    try:
        return service.revert(change_id, actor="user").to_dict()
    except MemoryServiceError as exc:
        raise MemoryViewError(str(exc)) from exc


__all__ = [
    "MemoryViewError",
    "confirm_fact",
    "create_fact",
    "forget",
    "project_memory",
    "revert_change",
    "update_commitment",
    "update_fact",
    "visible_spaces",
]
