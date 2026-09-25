"""Domain models, types, and validation for episodic memory (case studies/experience)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ....base.value_object import ValueObject

EpisodeStatus = Literal["completed", "failed", "max_steps"]
EpisodeOutcome = Literal["SUCCESS", "FAILURE", "PARTIAL"]

MAX_EPISODE_GOAL_CHARS = 2_000
MAX_EPISODE_OUTCOME_CHARS = 4_000
MAX_EPISODE_TOOLS = 100
MAX_EPISODE_VERIFICATIONS = 100
MAX_EPISODE_AGENTS = 64
_EPISODE_ID_RE = re.compile(r"ep-[A-Za-z0-9_-]{1,180}")


class EpisodeStoreError(ValueError):
    pass


class EpisodeNotFoundError(EpisodeStoreError):
    pass


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise EpisodeStoreError(f"{field} 必须是字符串")
    return value


def _bounded_string(
    value: Any,
    field: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
) -> str:
    text = _string(value, field, allow_empty=allow_empty)
    if len(text) > max_chars:
        raise EpisodeStoreError(f"{field} 不能超过 {max_chars} 个字符")
    return text


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EpisodeStoreError(f"{field} 必须是非负整数")
    return value


@dataclass(frozen=True)
class Episode(ValueObject):
    """Domain value object representing one episodic case study (task -> error -> resolution -> outcome)."""

    id: str
    task_description: str
    trigger_error: str | None = None
    resolution: str = ""
    outcome: EpisodeOutcome = "SUCCESS"
    token_count: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class EpisodeRecord:
    id: str
    session_id: str
    goal: str
    status: EpisodeStatus
    outcome: str
    started_step: int
    ended_step: int
    created_at: str
    plan: dict[str, Any]
    tools: tuple[dict[str, Any], ...]
    agents: tuple[dict[str, Any], ...]
    verification: tuple[dict[str, Any], ...]
    usage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "goal": self.goal,
            "status": self.status,
            "outcome": self.outcome,
            "started_step": self.started_step,
            "ended_step": self.ended_step,
            "created_at": self.created_at,
            "plan": self.plan,
            "tools": list(self.tools),
            "agents": list(self.agents),
            "verification": list(self.verification),
            "usage": dict(self.usage),
        }

    def to_episode(self) -> Episode:
        """Convert detailed storage record into domain Episode value object."""
        outcome_map: dict[EpisodeStatus, EpisodeOutcome] = {
            "completed": "SUCCESS",
            "failed": "FAILURE",
            "max_steps": "PARTIAL",
        }
        return Episode(
            id=self.id,
            task_description=self.goal,
            trigger_error=None if self.status == "completed" else self.outcome,
            resolution=self.outcome,
            outcome=outcome_map.get(self.status, "SUCCESS"),
            token_count=self.usage.get("total_tokens", 0),
            created_at=self.created_at,
        )

    @classmethod
    def from_dict(cls, value: Any) -> EpisodeRecord:
        if not isinstance(value, dict):
            raise EpisodeStoreError("episode 必须是对象")
        episode_id = value.get("id")
        if not isinstance(episode_id, str) or _EPISODE_ID_RE.fullmatch(episode_id) is None:
            raise EpisodeStoreError("episode id 非法")
        status = value.get("status")
        if status not in {"completed", "failed", "max_steps"}:
            raise EpisodeStoreError("episode status 非法")
        tools = value.get("tools", [])
        agents = value.get("agents", [])
        verification = value.get("verification", [])
        plan = value.get("plan", {})
        usage = value.get("usage", {})
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise EpisodeStoreError("episode tools 非法")
        if len(tools) > MAX_EPISODE_TOOLS:
            raise EpisodeStoreError("episode tools 超出上限")
        if not isinstance(agents, list) or not all(
            isinstance(item, dict) for item in agents
        ):
            raise EpisodeStoreError("episode agents 非法")
        if len(agents) > MAX_EPISODE_AGENTS:
            raise EpisodeStoreError("episode agents 超出上限")
        if not isinstance(verification, list) or not all(
            isinstance(item, dict) for item in verification
        ):
            raise EpisodeStoreError("episode verification 非法")
        if len(verification) > MAX_EPISODE_VERIFICATIONS:
            raise EpisodeStoreError("episode verification 超出上限")
        if not isinstance(plan, dict) or not isinstance(usage, dict):
            raise EpisodeStoreError("episode plan/usage 非法")
        started_step = _nonnegative_int(value.get("started_step"), "started_step")
        ended_step = _nonnegative_int(value.get("ended_step"), "ended_step")
        if ended_step < started_step:
            raise EpisodeStoreError("ended_step 不能小于 started_step")
        return cls(
            id=episode_id,
            session_id=_bounded_string(value.get("session_id"), "session_id", 128),
            goal=_bounded_string(
                value.get("goal"), "goal", MAX_EPISODE_GOAL_CHARS, allow_empty=True
            ),
            status=status,
            outcome=_bounded_string(
                value.get("outcome"),
                "outcome",
                MAX_EPISODE_OUTCOME_CHARS,
                allow_empty=True,
            ),
            started_step=started_step,
            ended_step=ended_step,
            created_at=_bounded_string(value.get("created_at"), "created_at", 100),
            plan=plan,
            tools=tuple(tools),
            agents=tuple(agents),
            verification=tuple(verification),
            usage={
                "prompt_tokens": _nonnegative_int(
                    usage.get("prompt_tokens", 0), "usage.prompt_tokens"
                ),
                "completion_tokens": _nonnegative_int(
                    usage.get("completion_tokens", 0), "usage.completion_tokens"
                ),
                "total_tokens": _nonnegative_int(
                    usage.get("total_tokens", 0), "usage.total_tokens"
                ),
            },
        )


__all__ = [
    "MAX_EPISODE_AGENTS",
    "MAX_EPISODE_GOAL_CHARS",
    "MAX_EPISODE_OUTCOME_CHARS",
    "MAX_EPISODE_TOOLS",
    "MAX_EPISODE_VERIFICATIONS",
    "Episode",
    "EpisodeNotFoundError",
    "EpisodeOutcome",
    "EpisodeRecord",
    "EpisodeStatus",
    "EpisodeStoreError",
]
