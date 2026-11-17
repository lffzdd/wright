"""Commitments: things the agent promised to do or remind the user about.

Unlike a fact, a commitment has a lifecycle and a due date, and the agent is
expected to bring it up when it is due rather than wait to be asked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

CommitmentStatus = Literal["open", "done", "dismissed", "expired"]
COMMITMENT_STATUSES = ("open", "done", "dismissed", "expired")
COMMITMENT_ID_RE = re.compile(r"c-[0-9a-f]{16}")
MAX_COMMITMENT_CHARS = 500
DUE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")


class CommitmentError(ValueError):
    pass


@dataclass(frozen=True)
class Commitment:
    id: str
    space_id: str
    content: str
    status: CommitmentStatus
    created_at: str
    updated_at: str
    # ISO date or datetime in local time; empty means no due date.
    due: str = ""
    source_episode: str = ""

    def evolve(self, **changes: Any) -> Commitment:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "content": self.content,
            "status": self.status,
            "due": self.due,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_episode": self.source_episode,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Commitment:
        if not isinstance(value, dict):
            raise CommitmentError("commitment must be an object")
        commitment_id = value.get("id")
        if not isinstance(commitment_id, str) or COMMITMENT_ID_RE.fullmatch(commitment_id) is None:
            raise CommitmentError("invalid commitment id")
        status = value.get("status")
        if status not in COMMITMENT_STATUSES:
            raise CommitmentError("invalid commitment status")
        return cls(
            id=commitment_id,
            space_id=str(value.get("space_id") or ""),
            content=str(value.get("content") or ""),
            status=status,
            due=str(value.get("due") or ""),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            source_episode=str(value.get("source_episode") or ""),
        )


def normalize_due(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if DUE_RE.fullmatch(text) is None:
        raise CommitmentError("due must look like YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    return text.replace(" ", "T")


__all__ = [
    "COMMITMENT_ID_RE",
    "COMMITMENT_STATUSES",
    "MAX_COMMITMENT_CHARS",
    "Commitment",
    "CommitmentError",
    "CommitmentStatus",
    "normalize_due",
]
