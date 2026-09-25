"""Data Transfer Objects for the Memory application service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...domain.model.episode import Episode
from ...domain.model.fact import Fact


@dataclass(frozen=True)
class MemoryContextDTO:
    """Consolidated memory context ready for agent prompt injection."""

    facts: tuple[Fact, ...] = ()
    episodes: tuple[Episode | Any, ...] = ()
    prompt_injection: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": [
                {
                    "id": f.id,
                    "key": f.key,
                    "content": f.content,
                    "scope": f.scope,
                    "category": f.category,
                }
                for f in self.facts
            ],
            "episodes": [
                {
                    "id": getattr(e, "id", ""),
                    "task": getattr(e, "task_description", "") or getattr(e, "goal", ""),
                    "resolution": getattr(e, "resolution", "") or getattr(e, "outcome", ""),
                    "outcome": getattr(e, "outcome", ""),
                }
                for e in self.episodes
            ],
            "prompt_injection": self.prompt_injection,
        }


__all__ = ["MemoryContextDTO"]
