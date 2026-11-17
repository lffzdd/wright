"""Value types for the egress check: what is leaving, and what it may contain.

Information levels (docs/memory.md):
- ``secret`` (S): credentials. Never leaves, not even with approval.
- ``sensitive`` (P2) and ``personal`` (P1): facts about the user or third parties.
- ``foreign_project``: technical facts that belong to a different repo space.
- ``shared_rules``: not a fact; a write to a team rules file the user did not ask for.

Sink levels:
- ``E0`` local and private: replies, private memory, files outside any project.
- ``E1`` the project working tree: likely to be committed.
- ``E2`` visible to other people: commits, pushes, PRs, issues, shared rules.
- ``E3`` third-party services: messages, form posts, search queries, URLs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

InfoLevel = Literal["secret", "sensitive", "personal", "foreign_project", "shared_rules"]
SinkLevel = Literal["E0", "E1", "E2", "E3"]
EgressAction = Literal["allow", "ask", "reject"]


@dataclass(frozen=True)
class EgressRequest:
    tool_name: str
    sink: SinkLevel
    text: str
    # The repo space the session is bound to. Its own P0 facts may leave.
    space_id: str = ""
    # Real user text of the current turn. Content the user typed this turn is
    # treated as authorized for this one action (rule R2).
    user_texts: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class EgressHit:
    level: InfoLevel
    reason: str
    snippet: str
    fact_id: str = ""
    space_id: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "reason": self.reason,
            "snippet": self.snippet,
            "fact_id": self.fact_id,
            "space_id": self.space_id,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class EgressVerdict:
    action: EgressAction
    hits: tuple[EgressHit, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @classmethod
    def allow(cls) -> EgressVerdict:
        return cls("allow")


__all__ = [
    "EgressAction",
    "EgressHit",
    "EgressRequest",
    "EgressVerdict",
    "InfoLevel",
    "SinkLevel",
]
