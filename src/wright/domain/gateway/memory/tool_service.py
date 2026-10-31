"""The memory operations model-facing tools call."""

from __future__ import annotations

from typing import Any, Protocol


class IMemoryToolService(Protocol):
    def get_core_memory(self, project_id: str = "") -> Any: ...

    def update_core_memory(
        self,
        section: str,
        content: str = "",
        mode: str = "append",
        *,
        project_id: str = "",
    ) -> tuple[Any, str | None]: ...

    def search_episodes(self, query: str = "", **options: Any) -> tuple[list[Any], str | None]: ...

    def get_episode(
        self, episode_id: str, *, include_evidence: bool = False
    ) -> tuple[Any, str | None]: ...

    def delete_episode(self, episode_id: str) -> tuple[Any, str | None]: ...

    def create_semantic(self, **fields: Any) -> tuple[Any, str | None]: ...

    def update_semantic(self, memory_id: str, **fields: Any) -> tuple[Any, str | None]: ...

    def delete_semantic(self, memory_id: str, **options: Any) -> tuple[Any, str | None]: ...

    def get_semantic(self, memory_id: str, **options: Any) -> tuple[Any, Any, str | None]: ...

    def search_semantic(self, query: str = "", **options: Any) -> tuple[list[Any], str | None]: ...
