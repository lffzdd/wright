"""Facts: the single store for long-lived knowledge.

What other systems split into "core" and "semantic" memory is one record type
here. ``tier`` decides how a fact reaches the model:

- ``pinned``: full text in every request.
- ``indexed``: one summary line in every request, full text on demand.
- ``archived``: only through search or turn recall.

``origin`` is decided by the write pipeline, never by the model. ``sensitivity``
drives the egress rules (P0/P1/P2 in docs/memory.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

FactKind = Literal["preference", "constraint", "fact", "feedback", "reference"]
FACT_KINDS = ("preference", "constraint", "fact", "feedback", "reference")

FactSubject = Literal["user", "project", "other"]
FACT_SUBJECTS = ("user", "project", "other")

# user_stated: the user said it in this conversation.
# user_confirmed: content came from elsewhere, the user explicitly asked to keep it.
# agent_inferred: the agent or the extractor concluded it.
# external: copied from a tool result (web page, file, mail); quarantined.
Origin = Literal["user_stated", "user_confirmed", "agent_inferred", "external"]
ORIGINS = ("user_stated", "user_confirmed", "agent_inferred", "external")
USER_ORIGINS = frozenset({"user_stated", "user_confirmed"})

# none = P0 (project/technical), personal = P1, sensitive = P2.
Sensitivity = Literal["none", "personal", "sensitive"]
SENSITIVITIES = ("none", "personal", "sensitive")
SENSITIVITY_RANK = {"none": 0, "personal": 1, "sensitive": 2}

Tier = Literal["pinned", "indexed", "archived"]
TIERS = ("pinned", "indexed", "archived")

FactStatus = Literal["active", "superseded", "quarantined", "deleted"]
FACT_STATUSES = ("active", "superseded", "quarantined", "deleted")

FACT_ID_RE = re.compile(r"f-[0-9a-f]{16}")
MAX_FACT_CHARS = 2_000
MAX_WHY_CHARS = 600
MAX_MARKERS = 12
MAX_MARKER_CHARS = 80


class FactError(ValueError):
    pass


def _choice(value: Any, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise FactError(f"invalid {name}: {value!r}")
    return str(value)


@dataclass(frozen=True)
class Fact:
    id: str
    space_id: str
    kind: FactKind
    subject: FactSubject
    content: str
    origin: Origin
    sensitivity: Sensitivity
    tier: Tier
    status: FactStatus
    created_at: str
    updated_at: str
    why: str = ""
    markers: tuple[str, ...] = ()
    # Episode ids and/or session locators this fact was learned from.
    sources: tuple[str, ...] = ()
    valid_from: str = ""
    valid_to: str = ""
    superseded_by: str = ""
    last_used_at: str = ""
    use_count: int = 0
    revision: int = 1
    # Set only through an explicit user action. Lets a P1/P2 fact leave the machine.
    shareable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        line = " ".join(self.content.split())
        return line if len(line) <= 120 else line[:117] + "…"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def evolve(self, **changes: Any) -> Fact:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "kind": self.kind,
            "subject": self.subject,
            "content": self.content,
            "why": self.why,
            "origin": self.origin,
            "sensitivity": self.sensitivity,
            "tier": self.tier,
            "status": self.status,
            "markers": list(self.markers),
            "sources": list(self.sources),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "superseded_by": self.superseded_by,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "revision": self.revision,
            "shareable": self.shareable,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Fact:
        if not isinstance(value, dict):
            raise FactError("fact must be an object")
        fact_id = value.get("id")
        if not isinstance(fact_id, str) or FACT_ID_RE.fullmatch(fact_id) is None:
            raise FactError("invalid fact id")
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise FactError("fact content is empty")
        return cls(
            id=fact_id,
            space_id=str(value.get("space_id") or ""),
            kind=_choice(value.get("kind"), FACT_KINDS, "kind"),  # type: ignore[arg-type]
            subject=_choice(value.get("subject", "other"), FACT_SUBJECTS, "subject"),  # type: ignore[arg-type]
            content=content,
            why=str(value.get("why") or ""),
            origin=_choice(value.get("origin"), ORIGINS, "origin"),  # type: ignore[arg-type]
            sensitivity=_choice(value.get("sensitivity"), SENSITIVITIES, "sensitivity"),  # type: ignore[arg-type]
            tier=_choice(value.get("tier"), TIERS, "tier"),  # type: ignore[arg-type]
            status=_choice(value.get("status"), FACT_STATUSES, "status"),  # type: ignore[arg-type]
            markers=tuple(str(item) for item in value.get("markers") or ()),
            sources=tuple(str(item) for item in value.get("sources") or ()),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            valid_from=str(value.get("valid_from") or ""),
            valid_to=str(value.get("valid_to") or ""),
            superseded_by=str(value.get("superseded_by") or ""),
            last_used_at=str(value.get("last_used_at") or ""),
            use_count=int(value.get("use_count") or 0),
            revision=int(value.get("revision") or 1),
            shareable=bool(value.get("shareable", False)),
        )


__all__ = [
    "FACT_ID_RE",
    "FACT_KINDS",
    "FACT_STATUSES",
    "FACT_SUBJECTS",
    "MAX_FACT_CHARS",
    "MAX_MARKERS",
    "MAX_MARKER_CHARS",
    "MAX_WHY_CHARS",
    "ORIGINS",
    "SENSITIVITIES",
    "SENSITIVITY_RANK",
    "TIERS",
    "USER_ORIGINS",
    "Fact",
    "FactError",
    "FactKind",
    "FactStatus",
    "FactSubject",
    "Origin",
    "Sensitivity",
    "Tier",
]
