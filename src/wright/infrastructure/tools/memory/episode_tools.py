"""Read/search/forget tools for immutable system-recorded episodes."""

from __future__ import annotations

from typing import Any

from ....application.memory.memory_service import MemoryService
from ....domain.model.memory import EPISODE_STATUSES
from ....domain.model.tool import ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime

_SCOPES = ("current_project", "all_projects", "legacy")


def search_episodes(
    query: str = "",
    status: str | None = None,
    limit: int = 20,
    scope: str = "current_project",
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService,
    project_id: str = "",
) -> ToolResult:
    del runtime
    if scope not in _SCOPES:
        return ToolResult.fail("episode scope 非法")
    views, error = service.search_episodes(
        query,
        status=status,
        limit=limit,
        scope=scope,  # type: ignore[arg-type]
        project_id=project_id if scope == "current_project" else "",
    )
    if error is not None:
        return ToolResult.fail(error)
    return ToolResult.success({
        "count": len(views),
        "scope": scope,
        "episodes": [view.summary_dict() for view in views],
    })


def get_episode(
    episode_id: str,
    include_evidence: bool = False,
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService,
    project_id: str = "",
) -> ToolResult:
    del runtime, project_id
    view, error = service.get_episode(episode_id, include_evidence=include_evidence)
    if error is not None or view is None or view.record is None:
        return ToolResult.fail(error or "episode 不存在")
    payload = view.record.to_dict()
    payload["project_source"] = view.project_source
    payload["verification_summary"] = view.verification_summary
    if include_evidence:
        payload["evidence_reads"] = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in view.evidence_reads
        ]
    return ToolResult.success(payload)


def delete_episode(
    episode_id: str,
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService,
    project_id: str = "",
) -> ToolResult:
    del runtime, project_id
    view, error = service.delete_episode(episode_id)
    if error is not None or view is None:
        return ToolResult.fail(error or "episode 不存在")
    return ToolResult.success({
        "message": "Episode deleted",
        "id": view.id,
        "goal": view.goal,
        "project_source": view.project_source,
    })


def _describe_episode_read(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("episode_id") or arguments.get("query") or ""),
        reason="read historical episode data",
    )


def _describe_episode_delete(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"persistent_write"}),
        subject=str(arguments.get("episode_id", "")),
        risk_flags=("persistent_state", "deletes_data"),
        reason="delete historical episode data",
    )


def build_episode_tools(
    service: MemoryService,
    *,
    project_id: str = "",
) -> list[Tool]:
    def bind(function):
        return lambda args, runtime: function(
            **args, runtime=runtime, service=service, project_id=project_id
        )

    return [
        Tool(
            name="search_episodes",
            description=(
                "Search past task episodes: goal, outcome, status, project source, "
                "and verification summary. Default scope is the current project. "
                "Use scope=all_projects or scope=legacy only when the user explicitly "
                "asks for other projects or older unscoped records. "
                "Episodes are historical experience; re-check current state before acting. "
                "lexical_score is a rank key, not a similarity probability."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": sorted(EPISODE_STATUSES),
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "scope": {
                        "type": "string",
                        "enum": list(_SCOPES),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            call=bind(search_episodes),
            access_descriptor=_describe_episode_read,
            is_concurrency_safe=lambda args: True,
            defer_to_model=True,
        ),
        Tool(
            name="get_episode",
            description=(
                "Read a full historical episode by episode_id, including plan, "
                "tool trace, and verification. Known ids include legacy records. "
                "Set include_evidence=true to read the registered source excerpts. "
                "That does not rerun tools or resume the old task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "episode_id": {"type": "string", "minLength": 1},
                    "include_evidence": {"type": "boolean"},
                },
                "required": ["episode_id"],
                "additionalProperties": False,
            },
            call=bind(get_episode),
            access_descriptor=_describe_episode_read,
            is_concurrency_safe=lambda args: True,
            defer_to_model=True,
        ),
        Tool(
            name="delete_episode",
            description=(
                "Delete a historical episode the user asked to forget. "
                "Models cannot create or edit episodes."
            ),
            parameters={
                "type": "object",
                "properties": {"episode_id": {"type": "string", "minLength": 1}},
                "required": ["episode_id"],
                "additionalProperties": False,
            },
            call=bind(delete_episode),
            access_descriptor=_describe_episode_delete,
            defer_to_model=True,
        ),
    ]
