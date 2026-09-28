"""Project memory and rule views through MemoryService.

Deletes require an explicit confirmation flag. The project id is taken from
the session's own root, not from an unrelated selected workspace.
"""

from __future__ import annotations

from typing import Any

from ...core.paths import project_id


class MemoryViewError(ValueError):
    pass


def project_memory(service: Any, *, bound_project_id: str) -> dict[str, Any]:
    core = None
    if service is not None:
        record = service.get_core_memory(bound_project_id)
        if record is not None and hasattr(record, "render_block"):
            core = {
                "project_id": record.project_id,
                "persona": record.persona,
                "human_profile": record.human_profile,
                "project_anchor": record.project_anchor,
                "project_anchor_state": record.project_anchor_state,
                "persona_editable": False,
            }
    semantic, error = ([], None)
    episodes = []
    episode_error = None
    if service is not None:
        semantic, error = service.search_semantic(
            "", limit=100, project_id=bound_project_id, include_inactive=True,
        )
        episodes, episode_error = service.search_episodes(
            "", limit=50, project_id=bound_project_id,
        )
    return {
        "project_id": bound_project_id,
        "core": core,
        "semantic": [_semantic_dict(item) for item in semantic],
        "semantic_error": error,
        "episodes": [_episode_dict(item) for item in episodes],
        "episode_error": episode_error,
    }


def update_core(service: Any, *, bound_project_id: str, section: str, content: str, mode: str) -> dict[str, Any]:
    _require_service(service)
    updated, error = service.update_core_memory(
        section, content, mode, project_id=bound_project_id,
    )
    if error:
        raise MemoryViewError(error)
    return {
        "section": updated.section,
        "content": updated.content,
        "scope": updated.scope,
        "project_id": updated.project_id or bound_project_id,
    }


def create_semantic(service: Any, *, bound_project_id: str, name: str, content: str, description: str, type_: str, scope: str) -> dict[str, Any]:
    _require_service(service)
    record, error = service.create_semantic(
        name=name,
        content=content,
        description=description,
        type_=type_,
        scope=scope,
        project_id=bound_project_id,
    )
    if error or record is None:
        raise MemoryViewError(error or "memory was not created")
    return _semantic_dict(record)


def update_semantic(service: Any, memory_id: str, *, bound_project_id: str, expected_revision: int, fields: dict[str, Any]) -> dict[str, Any]:
    _require_service(service)
    record, error = service.update_semantic(
        memory_id,
        expected_revision=expected_revision,
        project_id=bound_project_id,
        **fields,
    )
    if error or record is None:
        raise MemoryViewError(error or "memory was not updated")
    return _semantic_dict(record)


def delete_semantic(service: Any, memory_id: str, *, bound_project_id: str, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise MemoryViewError("confirmation is required")
    _require_service(service)
    record, error = service.delete_semantic(memory_id, project_id=bound_project_id)
    if error or record is None:
        raise MemoryViewError(error or "memory was not deleted")
    return {"id": memory_id, "deleted": True, "project_id": bound_project_id}


def delete_episode(service: Any, episode_id: str, *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise MemoryViewError("confirmation is required")
    _require_service(service)
    record, error = service.delete_episode(episode_id)
    if error:
        raise MemoryViewError(error)
    return {"id": episode_id, "deleted": record is not None}


def bound_project(session: Any) -> str:
    root = getattr(session, "project_root", None) or getattr(session, "workspace_dir", None)
    if root is None:
        raise MemoryViewError("session has no project root")
    return project_id(root)


def _require_service(service: Any) -> None:
    if service is None:
        raise MemoryViewError("memory service is not available")


def _semantic_dict(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        return record.to_dict()
    return {
        "id": getattr(record, "id", ""),
        "name": getattr(record, "name", ""),
        "content": getattr(record, "content", ""),
        "description": getattr(record, "description", ""),
        "type": getattr(record, "type", ""),
        "project_id": getattr(record, "project_id", ""),
        "revision": getattr(record, "revision", None),
        "status": getattr(record, "status", ""),
    }


def _episode_dict(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        data = record.to_dict()
        if isinstance(data, dict):
            return data
    return {
        "id": getattr(record, "id", ""),
        "title": getattr(record, "title", "") or getattr(record, "goal", ""),
        "status": getattr(record, "status", ""),
    }


__all__ = [
    "MemoryViewError",
    "bound_project",
    "create_semantic",
    "delete_episode",
    "delete_semantic",
    "project_memory",
    "update_core",
    "update_semantic",
]
