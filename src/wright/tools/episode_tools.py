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
            "message": "episode 已删除",
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
        f"删除历史 episode {arguments.get('episode_id', '')}",
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
                "搜索过去任务的执行经历，包括目标、结果和完成状态。episode 是历史经验，"
                "使用前仍需核实当前状态。"
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
        ),
        Tool(
            name="get_episode",
            description="按 episode_id 读取完整历史执行记录，包括计划、工具轨迹和验证结果。",
            parameters={
                "type": "object",
                "properties": {"episode_id": {"type": "string", "minLength": 1}},
                "required": ["episode_id"],
                "additionalProperties": False,
            },
            call=bind(get_episode),
            is_concurrency_safe=lambda args: True,
        ),
        Tool(
            name="delete_episode",
            description="删除用户明确要求忘记的历史 episode。episode 不支持模型手工创建或修改。",
            parameters={
                "type": "object",
                "properties": {"episode_id": {"type": "string", "minLength": 1}},
                "required": ["episode_id"],
                "additionalProperties": False,
            },
            call=bind(delete_episode),
            check_permission=_delete_permission,
        ),
    ]
