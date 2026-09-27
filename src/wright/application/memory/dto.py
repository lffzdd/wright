"""Data Transfer Objects for the Memory application service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.model.memory import CoreMemory, EpisodeRecord, SemanticMemoryRecord


@dataclass(frozen=True)
class CoreMemoryUpdateDTO:
    """Section and content saved by one successful core memory update."""

    section: str
    content: str
    scope: str = ""
    project_id: str = ""


@dataclass(frozen=True)
class EpisodeViewDTO:
    """Application view of one episode. Tools return this, not a store object."""

    id: str
    project_id: str
    project_source: str
    created_at: str
    goal: str
    status: str
    outcome: str
    termination_reason: str
    verification_summary: str
    lexical_score: float | None = None
    record: EpisodeRecord | None = None
    evidence_reads: tuple[Any, ...] = ()

    def summary_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "project_source": self.project_source,
            "created_at": self.created_at,
            "goal": self.goal,
            "status": self.status,
            "outcome": self.outcome,
            "termination_reason": self.termination_reason,
            "verification_summary": self.verification_summary,
        }
        if self.lexical_score is not None:
            payload["lexical_score"] = self.lexical_score
        return payload


@dataclass(frozen=True)
class MemoryContextDTO:
    """Recall prepared for one user turn.

    Core memory stays on this object for callers that read it directly.
    Request injection uses ``semantic_text`` and ``episode_text`` separately
    so a tight context can drop episodes without dropping the user turn.
    """

    core_memory: CoreMemory | None = None
    memories: tuple[SemanticMemoryRecord, ...] = ()
    episodes: tuple[EpisodeRecord, ...] = ()
    semantic_text: str = ""
    episode_text: str = ""
    selected_memory_ids: tuple[str, ...] = ()
    selected_episode_ids: tuple[str, ...] = ()
    episode_estimated_tokens: int = 0
    semantic_estimated_tokens: int = 0
    rendered_episode_ids: tuple[str, ...] = ()
    budget_skipped_episode_ids: tuple[str, ...] = ()
    prompt_injection: str = ""
    project_id: str = ""
    selector_attempted: bool = False
    selector_failed: bool = False
    selector_failure_type: str = ""
    semantic_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "core_memory": self.core_memory.to_dict() if self.core_memory is not None else None,
            "memories": [
                {
                    "id": record.id,
                    "name": record.name,
                    "type": record.type,
                    "description": record.description,
                    "content": record.content,
                }
                for record in self.memories
            ],
            "episodes": [
                {
                    "id": episode.id,
                    "goal": episode.goal,
                    "status": episode.status,
                    "outcome": episode.outcome,
                }
                for episode in self.episodes
            ],
            "semantic_text": self.semantic_text,
            "episode_text": self.episode_text,
            "selected_memory_ids": list(self.selected_memory_ids),
            "selected_episode_ids": list(self.selected_episode_ids),
            "episode_estimated_tokens": self.episode_estimated_tokens,
            "semantic_estimated_tokens": self.semantic_estimated_tokens,
            "rendered_episode_ids": list(self.rendered_episode_ids),
            "budget_skipped_episode_ids": list(self.budget_skipped_episode_ids),
            "prompt_injection": self.prompt_injection,
            "project_id": self.project_id,
            "selector_attempted": self.selector_attempted,
            "selector_failed": self.selector_failed,
            "selector_failure_type": self.selector_failure_type,
            "semantic_generation": self.semantic_generation,
        }


__all__ = [
    "CoreMemoryUpdateDTO",
    "EpisodeViewDTO",
    "MemoryContextDTO",
]
