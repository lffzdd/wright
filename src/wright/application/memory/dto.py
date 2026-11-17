"""Data transfer objects returned by the memory application service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...domain.model.memory import EpisodeRecord, Fact


@dataclass(frozen=True)
class WriteRequest:
    """One proposed fact. ``origin`` and ``sensitivity`` are not inputs."""

    content: str
    kind: str
    space_id: str
    subject: str = "other"
    why: str = ""
    markers: tuple[str, ...] = ()
    pin: bool = False
    # The model may raise the sensitivity it would otherwise get, never lower it.
    sensitivity_hint: str = ""
    supersedes: str = ""
    sources: tuple[str, ...] = ()
    actor: str = "agent"
    # Real user text and tool results of the current turn, for origin inference.
    user_texts: tuple[str, ...] = ()
    external_texts: tuple[str, ...] = ()
    force_new: bool = False


@dataclass(frozen=True)
class WriteOutcome:
    # created | duplicate | quarantined | rejected | needs_user
    status: str
    fact: Fact | None = None
    message: str = ""
    related: tuple[Fact, ...] = ()
    superseded: str = ""
    moved_to: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"created", "quarantined"}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.fact is not None:
            payload["fact"] = self.fact.to_dict()
        if self.related:
            payload["related"] = [{"id": item.id, "summary": item.summary} for item in self.related]
        if self.superseded:
            payload["superseded"] = self.superseded
        if self.moved_to:
            payload["moved_to"] = self.moved_to
        return payload


@dataclass(frozen=True)
class SearchHit:
    id: str
    type: str  # fact | episode | commitment
    space_id: str
    summary: str
    score: float
    created_at: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "space_id": self.space_id,
            "summary": self.summary,
            "score": round(self.score, 4),
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class ResidentView:
    """The block every request carries: pinned facts, the index, due commitments."""

    text: str = ""
    pinned_ids: tuple[str, ...] = ()
    indexed_ids: tuple[str, ...] = ()
    commitment_ids: tuple[str, ...] = ()
    estimated_tokens: int = 0


@dataclass(frozen=True)
class MemoryContextDTO:
    """Recall prepared for one user turn.

    ``fact_text`` is required context. ``episode_text`` is optional and may be
    dropped by the context builder when the window is tight.
    """

    facts: tuple[Fact, ...] = ()
    episodes: tuple[EpisodeRecord, ...] = ()
    fact_text: str = ""
    episode_text: str = ""
    selected_fact_ids: tuple[str, ...] = ()
    selected_episode_ids: tuple[str, ...] = ()
    rendered_episode_ids: tuple[str, ...] = ()
    budget_skipped_episode_ids: tuple[str, ...] = ()
    fact_estimated_tokens: int = 0
    episode_estimated_tokens: int = 0
    space_ids: tuple[str, ...] = ()
    project_id: str = ""
    selector_attempted: bool = False
    selector_failed: bool = False
    selector_failure_type: str = ""
    generation: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": [{"id": item.id, "summary": item.summary, "space_id": item.space_id} for item in self.facts],
            "episodes": [
                {"id": item.id, "goal": item.goal, "status": item.status, "outcome": item.outcome}
                for item in self.episodes
            ],
            "fact_text": self.fact_text,
            "episode_text": self.episode_text,
            "selected_fact_ids": list(self.selected_fact_ids),
            "selected_episode_ids": list(self.selected_episode_ids),
            "rendered_episode_ids": list(self.rendered_episode_ids),
            "budget_skipped_episode_ids": list(self.budget_skipped_episode_ids),
            "fact_estimated_tokens": self.fact_estimated_tokens,
            "episode_estimated_tokens": self.episode_estimated_tokens,
            "space_ids": list(self.space_ids),
            "project_id": self.project_id,
            "selector_attempted": self.selector_attempted,
            "selector_failed": self.selector_failed,
            "selector_failure_type": self.selector_failure_type,
            "generation": self.generation,
        }


__all__ = [
    "MemoryContextDTO",
    "ResidentView",
    "SearchHit",
    "WriteOutcome",
    "WriteRequest",
]
