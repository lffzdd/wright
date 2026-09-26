"""Read registered episode evidence from a session. No search and no execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from ...model.memory.episode import EvidenceRef


@dataclass(frozen=True)
class EvidenceRead:
    """One on-demand read of a locator stored on an episode.

    ``available`` means that exact locator was found. ``unavailable`` means the
    session or the registered source is gone. ``rejected`` means the locator
    does not belong to the recorded session or run. None of these statuses
    is a guess at a similar message.
    """

    evidence_id: str
    status: str
    kind: str = ""
    text: str = ""
    truncated: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "status": self.status,
            "kind": self.kind,
            "text": self.text,
            "truncated": self.truncated,
            "reason": self.reason,
        }


class IEpisodeEvidenceSource(ABC):
    """Load only the locators the caller passes. Do not run tools or resume work."""

    @abstractmethod
    def load(self, refs: Sequence[EvidenceRef]) -> Sequence[EvidenceRead]:
        """Read each ref at most once per session load."""
        ...


__all__ = ["EvidenceRead", "IEpisodeEvidenceSource"]
