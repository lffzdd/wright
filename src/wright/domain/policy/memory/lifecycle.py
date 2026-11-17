"""Ranking and lifecycle rules: what stays resident, what decays, what is dropped.

All thresholds are starting values, not tuned numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ...model.memory import Commitment, EpisodeRecord, Fact, space_type_of

QUARANTINE_DAYS = 7
INDEXED_IDLE_DAYS = 90
INFERRED_IDLE_DAYS = 60
INFERRED_DELETE_DAYS = 180
PROMOTE_SOURCES = 2
PROMOTE_USES = 3
EPISODE_COMPACT_DAYS = 30
COMMITMENT_EXPIRE_DAYS = 14
COMMITMENT_ARCHIVE_DAYS = 30
COMMITMENT_HORIZON_DAYS = 7
RECENCY_HALF_LIFE_DAYS = 30.0

_ORIGIN_WEIGHT = {
    "user_stated": 1.0,
    "user_confirmed": 0.9,
    "agent_inferred": 0.6,
    "external": 0.0,
}
_SPACE_WEIGHT = {"repo": 1.2, "personal": 1.1, "global": 1.0}


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_days(value: str, now: datetime) -> float:
    moment = parse_time(value)
    if moment is None:
        return 0.0
    return max(0.0, (now - moment).total_seconds() / 86_400)


def last_touch(fact: Fact) -> str:
    return fact.last_used_at or fact.updated_at or fact.created_at


def index_score(fact: Fact, now: datetime) -> float:
    try:
        space_weight = _SPACE_WEIGHT[space_type_of(fact.space_id)]
    except Exception:
        space_weight = 1.0
    recency = 0.5 ** (_age_days(last_touch(fact), now) / RECENCY_HALF_LIFE_DAYS)
    return space_weight * (0.25 + recency) * math.log(2 + fact.use_count) * _ORIGIN_WEIGHT.get(fact.origin, 0.5)


@dataclass(frozen=True)
class FactAction:
    action: str  # "delete" | "demote" | "promote"
    reason: str


def fact_lifecycle(fact: Fact, now: datetime) -> FactAction | None:
    if fact.status == "quarantined":
        if _age_days(fact.created_at, now) > QUARANTINE_DAYS:
            return FactAction("delete", "quarantine_expired")
        return None
    if fact.status != "active":
        return None
    idle = _age_days(last_touch(fact), now)
    inferred = fact.origin == "agent_inferred"
    if inferred and fact.tier == "archived":
        episode_sources = [item for item in fact.sources if item.startswith("ep-")]
        if len(set(episode_sources)) >= PROMOTE_SOURCES or fact.use_count >= PROMOTE_USES:
            return FactAction("promote", "repeated_evidence")
        if idle > INFERRED_DELETE_DAYS:
            return FactAction("delete", "inferred_unused")
        return None
    if fact.tier == "indexed" and idle > (INFERRED_IDLE_DAYS if inferred else INDEXED_IDLE_DAYS):
        return FactAction("demote", "idle")
    return None


def episode_should_compact(episode: EpisodeRecord, now: datetime) -> bool:
    return not episode.compacted and _age_days(episode.created_at, now) > EPISODE_COMPACT_DAYS


def commitment_lifecycle(item: Commitment, now: datetime) -> str | None:
    """Return "expire" or "delete" when a commitment should move on."""
    if item.status == "open":
        due = due_time(item.due)
        if due is not None and now - due > timedelta(days=COMMITMENT_EXPIRE_DAYS):
            return "expire"
        return None
    if _age_days(item.updated_at, now) > COMMITMENT_ARCHIVE_DAYS:
        return "delete"
    return None


def due_time(due: str) -> datetime | None:
    if not due:
        return None
    moment = parse_time(due if "T" in due else f"{due}T23:59:59")
    return moment


def is_due_soon(item: Commitment, now: datetime, *, horizon_days: int = COMMITMENT_HORIZON_DAYS) -> bool:
    if item.status != "open":
        return False
    due = due_time(item.due)
    if due is None:
        return False
    return due - now <= timedelta(days=horizon_days)


__all__ = [
    "COMMITMENT_HORIZON_DAYS",
    "EPISODE_COMPACT_DAYS",
    "INDEXED_IDLE_DAYS",
    "QUARANTINE_DAYS",
    "FactAction",
    "commitment_lifecycle",
    "due_time",
    "episode_should_compact",
    "fact_lifecycle",
    "index_score",
    "is_due_soon",
    "last_touch",
    "parse_time",
]
