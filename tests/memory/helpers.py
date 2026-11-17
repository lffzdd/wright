"""Shared builders for memory tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from wright.application.memory.dto import WriteRequest
from wright.application.memory.service import MemoryService
from wright.domain.gateway.memory import IContextSelector, SelectorChoice
from wright.domain.model.memory import EpisodeRecord, repo_space_id
from wright.infrastructure.persistence.memory import SqliteMemoryStore


class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now = self.now + timedelta(**delta)


class ScriptedSelector(IContextSelector):
    def __init__(self, facts=(), episodes=(), failed=False) -> None:
        self.facts = tuple(facts)
        self.episodes = tuple(episodes)
        self.failed = failed
        self.calls: list[dict[str, str]] = []

    def select(self, *, task, fact_manifest, episode_manifest) -> SelectorChoice:
        self.calls.append({"task": task, "facts": fact_manifest, "episodes": episode_manifest})
        if self.failed:
            return SelectorChoice(failed=True, episodes_usable=False, failure_type="boom")
        return SelectorChoice(fact_ids=self.facts, episode_ids=self.episodes)


def make_service(tmp_path: Path, *, clock: Clock | None = None, selector=None, project: str = "proj-a") -> MemoryService:
    service = MemoryService(SqliteMemoryStore(tmp_path / "memory"), clock=clock or Clock(), selector=selector)
    if project:
        service.ensure_repo_space(project, f"/work/{project}")
    return service


def write(service: MemoryService, content: str, *, kind="fact", space=None, actor="user", **options):
    return service.write_fact(WriteRequest(
        content=content,
        kind=kind,
        space_id=space or repo_space_id("proj-a"),
        actor=actor,
        **options,
    ))


def episode(episode_id: str, *, project: str = "proj-a", goal: str = "goal", outcome: str = "done",
            session_id: str = "s-1", created_at: str = "2026-10-30T10:00:00+00:00") -> EpisodeRecord:
    return EpisodeRecord(
        id=episode_id,
        session_id=session_id,
        goal=goal,
        status="completed",
        outcome=outcome,
        started_step=0,
        ended_step=1,
        created_at=created_at,
        plan={},
        tools=({"name": "shell", "status": "succeeded"},),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        project_id=project,
        project_root=f"/work/{project}",
        root_run_id=f"run-{episode_id}",
    )
