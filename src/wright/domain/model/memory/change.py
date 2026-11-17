"""Change log entries. Every write to memory leaves one, so it can be reviewed and undone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ChangeActor = Literal["agent", "user", "extractor", "consolidation"]
CHANGE_ACTORS = ("agent", "user", "extractor", "consolidation")


@dataclass(frozen=True)
class ChangeRecord:
    id: int
    at: str
    actor: ChangeActor
    action: str
    target_id: str
    # JSON snapshots of the target before and after. Empty before means "created".
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    note: str = ""
    reverted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "actor": self.actor,
            "action": self.action,
            "target_id": self.target_id,
            "before": self.before,
            "after": self.after,
            "note": self.note,
            "reverted": self.reverted,
        }


__all__ = ["CHANGE_ACTORS", "ChangeActor", "ChangeRecord"]
