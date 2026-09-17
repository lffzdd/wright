"""Read/search/forget tools for immutable system-recorded episodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..memory.episode import EpisodeStore, EpisodeStoreError
from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime


def search_episodes(
    query: str = "",
    status: str | None = None,
    limit: int = 20,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        episodes = EpisodeStore(directory).search(
            query, status=status, limit=limit  # type: ignore[arg-type]
        )
        return ToolResult.success({
            "count": len(episodes),
            "episodes": [
                {
                    "id": episode.id,
                    "created_at": episode.created_at,
                    "goal": episode.goal,
                    "status": episode.status,
                    "outcome": episode.outcome,
                }
                for episode in episodes
            ],
        })
    except EpisodeStoreError as exc:
        return ToolResult.fail(str(exc))


def get_episode(
    episode_id: str,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(EpisodeStore(directory).get(episode_id).to_dict())
    except EpisodeStoreError as exc:
        return ToolResult.fail(str(exc))


def delete_episode(
    episode_id: str,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        episode = EpisodeStore(directory).delete(episode_id)
        return ToolResult.success({
            "message": "Episode deleted",
            "id": episode.id,
            "goal": episode.goal,
        })
    except EpisodeStoreError as exc:
        return ToolResult.fail(str(exc))


def _delete_permission(
    arguments: dict[str, Any], runtime: ToolRuntime
) -> PermissionCheckResult:
    return PermissionCheckResult(
        "ask",
        f"Delete historical episode {arguments.get('episode_id', '')}",
        ("deletes_data",),
        source="episode_tool",
    )


def build_episode_tools(directory: Path | None = None) -> list[Tool]:
    def bind(function):
        return lambda args, runtime: function(
            **args, runtime=runtime, directory=directory
        )

    return [
        Tool(
            name="search_episodes",
            description=(
                "Search past task episodes: goal, outcome, and completion status. "
                "Episodes are historical experience; re-check current state before acting on them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["completed", "failed", "max_steps"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [],
                "additionalProperties": False,
            },
            call=bind(search_episodes),
            is_concurrency_safe=lambda args: True,
            defer_to_model=True,
        ),
        Tool(
            name="get_episode",
            description="Read a full historical episode by episode_id, including plan, tool trace, and verification.",
            parameters={
                "type": "object",
                "properties": {"episode_id": {"type": "string", "minLength": 1}},
                "required": ["episode_id"],
                "additionalProperties": False,
            },
            call=bind(get_episode),
            is_concurrency_safe=lambda args: True,
            defer_to_model=True,
        ),
        Tool(
            name="delete_episode",
            description="Delete a historical episode the user asked to forget. Models cannot create or edit episodes.",
            parameters={
                "type": "object",
                "properties": {"episode_id": {"type": "string", "minLength": 1}},
                "required": ["episode_id"],
                "additionalProperties": False,
            },
            call=bind(delete_episode),
            check_permission=_delete_permission,
            defer_to_model=True,
        ),
    ]
