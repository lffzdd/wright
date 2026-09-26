"""Domain models, types, and validation for episode memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

EpisodeStatus = Literal["completed", "failed", "cancelled", "max_steps"]
EPISODE_STATUSES = frozenset({"completed", "failed", "cancelled", "max_steps"})
# New records are failed + termination_reason=max_steps. max_steps stays readable.
EPISODE_ID_RE = re.compile(r"ep-[A-Za-z0-9_-]{1,180}")
EPISODE_SCHEMA_VERSION = 3
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,180}")
EpisodeSearchScope = Literal["current_project", "all_projects", "legacy"]
EPISODE_SEARCH_SCOPES = frozenset({"current_project", "all_projects", "legacy"})
SourceKind = Literal[
    "user_statement",
    "tool_observation",
    "assistant_statement",
    "verification_record",
]
SOURCE_KINDS = frozenset({
    "user_statement",
    "tool_observation",
    "assistant_statement",
    "verification_record",
})
EVIDENCE_ID_RE = re.compile(r"ev-[A-Za-z0-9_-]{1,160}")

MAX_EPISODE_GOAL_CHARS = 2_000
MAX_EPISODE_OUTCOME_CHARS = 4_000
MAX_EPISODE_TOOLS = 100
MAX_EPISODE_VERIFICATIONS = 100
MAX_EPISODE_AGENTS = 64
MAX_EPISODE_EVIDENCE = 80
MAX_EVIDENCE_TEXT = 500


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
class EvidenceRef:
    """Locator for one source that already belongs to an episode.

    ``kind`` is who produced the text. ``execution_status`` and ``ok`` are the
    tool runtime result when the source is a tool call. Neither field means the
    user's goal was achieved, and neither field means a test passed.
    Missing evidence on an older episode means the source was not recorded.
    """

    id: str
    kind: SourceKind
    session_id: str
    root_run_id: str
    run_id: str = ""
    message_id: str = ""
    tool_call_id: str = ""
    step_id: str = ""
    summary: str = ""
    command: str = ""
    subject: str = ""
    error_excerpt: str = ""
    execution_status: str = ""
    ok: bool | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "root_run_id": self.root_run_id,
            "run_id": self.run_id,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id,
            "step_id": self.step_id,
            "summary": self.summary,
            "command": self.command,
            "subject": self.subject,
            "error_excerpt": self.error_excerpt,
            "execution_status": self.execution_status,
        }
        if self.ok is not None:
            payload["ok"] = self.ok
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceRef:
        if not isinstance(value, dict):
            raise EpisodeStoreError("evidence 必须是对象")
        evidence_id = value.get("id")
        if not isinstance(evidence_id, str) or EVIDENCE_ID_RE.fullmatch(evidence_id) is None:
            raise EpisodeStoreError("evidence id 非法")
        kind = value.get("kind")
        if kind not in SOURCE_KINDS:
            raise EpisodeStoreError("evidence kind 非法")
        ok = value.get("ok", None)
        if ok is not None and not isinstance(ok, bool):
            raise EpisodeStoreError("evidence ok 必须是布尔或空")
        exit_code = value.get("exit_code", None)
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise EpisodeStoreError("evidence exit_code 必须是整数或空")
        return cls(
            id=evidence_id,
            kind=kind,
            session_id=_bounded_string(value.get("session_id"), "evidence.session_id", 128),
            root_run_id=_bounded_string(
                value.get("root_run_id", ""),
                "evidence.root_run_id",
                200,
                allow_empty=True,
            ),
            run_id=_bounded_string(
                value.get("run_id", ""), "evidence.run_id", 200, allow_empty=True
            ),
            message_id=_bounded_string(
                value.get("message_id", ""), "evidence.message_id", 200, allow_empty=True
            ),
            tool_call_id=_bounded_string(
                value.get("tool_call_id", ""),
                "evidence.tool_call_id",
                200,
                allow_empty=True,
            ),
            step_id=_bounded_string(
                value.get("step_id", ""), "evidence.step_id", 200, allow_empty=True
            ),
            summary=_bounded_string(
                value.get("summary", ""),
                "evidence.summary",
                MAX_EVIDENCE_TEXT,
                allow_empty=True,
            ),
            command=_bounded_string(
                value.get("command", ""),
                "evidence.command",
                MAX_EVIDENCE_TEXT,
                allow_empty=True,
            ),
            subject=_bounded_string(
                value.get("subject", ""),
                "evidence.subject",
                MAX_EVIDENCE_TEXT,
                allow_empty=True,
            ),
            error_excerpt=_bounded_string(
                value.get("error_excerpt", ""),
                "evidence.error_excerpt",
                MAX_EVIDENCE_TEXT,
                allow_empty=True,
            ),
            execution_status=_bounded_string(
                value.get("execution_status", ""),
                "evidence.execution_status",
                40,
                allow_empty=True,
            ),
            ok=ok,
            exit_code=exit_code,
        )


@dataclass(frozen=True)
class EpisodeSearchHit:
    """One lexical search hit. ``lexical_score`` ranks candidates; it is not a probability."""

    episode: EpisodeRecord
    lexical_score: float


def _parse_evidence(value: Any) -> tuple[EvidenceRef, ...]:
    """Missing evidence means an older record did not store source locators."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EpisodeStoreError("episode evidence 非法")
    if len(value) > MAX_EPISODE_EVIDENCE:
        raise EpisodeStoreError("episode evidence 超出上限")
    refs = tuple(EvidenceRef.from_dict(item) for item in value)
    ids = [item.id for item in refs]
    if len(ids) != len(set(ids)):
        raise EpisodeStoreError("episode evidence id 重复")
    return refs


@dataclass(frozen=True)
class EpisodeRecord:
    """One finished user turn: goal, terminal status, answer text, and execution trace.

    Version 1 records may omit project fields. An empty ``project_id`` means the
    project is unknown (legacy flat files). Version 2 adds project identity.
    Version 3 adds evidence locators. Missing ``evidence`` stays an empty tuple
    and is not backfilled.
    """

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
    version: int = 1
    project_id: str = ""
    project_root: str = ""
    root_run_id: str = ""
    termination_reason: str = ""
    evidence: tuple[EvidenceRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "project_root": self.project_root,
            "root_run_id": self.root_run_id,
            "goal": self.goal,
            "status": self.status,
            "termination_reason": self.termination_reason,
            "outcome": self.outcome,
            "started_step": self.started_step,
            "ended_step": self.ended_step,
            "created_at": self.created_at,
            "plan": self.plan,
            "tools": list(self.tools),
            "agents": list(self.agents),
            "verification": list(self.verification),
            "evidence": [item.to_dict() for item in self.evidence],
            "usage": dict(self.usage),
        }

    @classmethod
    def from_dict(cls, value: Any) -> EpisodeRecord:
        if not isinstance(value, dict):
            raise EpisodeStoreError("episode 必须是对象")
        episode_id = value.get("id")
        if not isinstance(episode_id, str) or EPISODE_ID_RE.fullmatch(episode_id) is None:
            raise EpisodeStoreError("episode id 非法")
        status = value.get("status")
        if status not in EPISODE_STATUSES:
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
        version = value.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise EpisodeStoreError("episode version 非法")
        project = value.get("project_id", "")
        if project is None:
            project = ""
        if not isinstance(project, str) or (
            project and PROJECT_ID_RE.fullmatch(project) is None
        ):
            raise EpisodeStoreError("episode project_id 非法")
        return cls(
            id=episode_id,
            session_id=_bounded_string(value.get("session_id"), "session_id", 128),
            version=version,
            project_id=project,
            project_root=_bounded_string(
                value.get("project_root", ""),
                "project_root",
                4_096,
                allow_empty=True,
            ),
            root_run_id=_bounded_string(
                value.get("root_run_id", ""),
                "root_run_id",
                200,
                allow_empty=True,
            ),
            termination_reason=_bounded_string(
                value.get("termination_reason", ""),
                "termination_reason",
                200,
                allow_empty=True,
            ),
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
            evidence=_parse_evidence(value["evidence"]) if "evidence" in value else (),
        )


def episode_fact_pieces(episode: EpisodeRecord) -> dict[str, str]:
    """Structured facts shared by search and selector summaries.

    Token counters are not included. An empty piece means the fact is unknown.
    """
    errors: list[str] = []
    objects: list[str] = []
    names: list[str] = []
    for tool in episode.tools:
        name = str(tool.get("name") or "").strip()
        if name:
            names.append(name)
        error = str(tool.get("error") or "").strip()
        if error:
            errors.append(error)
    for agent in episode.agents:
        task = str(agent.get("task") or "").strip()
        if task:
            names.append(task)
        error = str(agent.get("error") or "").strip()
        if error:
            errors.append(error)
    for item in episode.evidence:
        if item.command:
            objects.append(item.command)
        if item.subject:
            objects.append(item.subject)
        if item.error_excerpt:
            errors.append(item.error_excerpt)
        if item.exit_code is not None:
            objects.append(f"exit={item.exit_code}")
    verification_lines: list[str] = []
    for item in episode.verification:
        approved = item.get("approved")
        issues = item.get("issues") or []
        issue_bits: list[str] = []
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    text = str(issue.get("message") or issue.get("code") or "")
                else:
                    text = str(issue)
                if text:
                    issue_bits.append(text[:200])
        line = f"approved={approved}"
        if issue_bits:
            line += " issues=" + "; ".join(issue_bits)
        verification_lines.append(line)
    return {
        "errors": " ; ".join(dict.fromkeys(errors))[:2_000],
        "objects": " ; ".join(dict.fromkeys(objects))[:2_000],
        "names": " ; ".join(dict.fromkeys(names))[:2_000],
        "verification": " | ".join(verification_lines)[:1_000],
    }


__all__ = [
    "EPISODE_ID_RE",
    "EPISODE_SCHEMA_VERSION",
    "EPISODE_SEARCH_SCOPES",
    "EPISODE_STATUSES",
    "EVIDENCE_ID_RE",
    "MAX_EPISODE_AGENTS",
    "MAX_EPISODE_EVIDENCE",
    "MAX_EPISODE_GOAL_CHARS",
    "MAX_EPISODE_OUTCOME_CHARS",
    "MAX_EPISODE_TOOLS",
    "MAX_EPISODE_VERIFICATIONS",
    "MAX_EVIDENCE_TEXT",
    "PROJECT_ID_RE",
    "SOURCE_KINDS",
    "EpisodeNotFoundError",
    "EpisodeRecord",
    "EpisodeSearchHit",
    "EpisodeSearchScope",
    "EpisodeStatus",
    "EpisodeStoreError",
    "EvidenceRef",
    "SourceKind",
    "episode_fact_pieces",
]
