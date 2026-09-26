"""Episode strategy contracts.

The scripted selector proves parsing, id checks, and injection boundaries.
It is not a relevance-quality evaluation of a real model.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.responses import event, response
from wright.application.agent import create_agent
from wright.application.agent.context import ContextBuilder, ContextCompactor
from wright.application.agent.subagent import make_spawn_agent_tool
from wright.application.memory import MemoryManager
from wright.application.memory.memory_service import MemoryService
from wright.application.memory.projection import render_episode_for_budget
from wright.core.paths import project_id
from wright.domain.gateway.memory import SelectorChoice
from wright.domain.model.events import ContentDelta, ContentDone, UsageEvent
from wright.domain.model.memory import EpisodeRecord
from wright.domain.model.session import MessageRecord, Session, UsageRecord
from wright.domain.model.tool import ToolCall, ToolResult
from wright.infrastructure.persistence.file_session_repo import FileSessionRepository
from wright.infrastructure.persistence.memory import (
    EpisodeStore,
    EpisodeStoreError,
    SemanticMemoryStore,
)
from wright.infrastructure.persistence.memory.selector import (
    SELECT_SYSTEM_PROMPT,
    LlmContextSelector,
)
from wright.infrastructure.tools.executor import ConcurrentToolExecutor
from wright.infrastructure.tools.memory import build_episode_tools
from wright.infrastructure.tools.runtime import ToolRuntime
from wright.interfaces.renderer import SilentRenderer
from wright.utils.token_counter import estimate_tokens


class _QuietLLM:
    def __call__(self, messages, **kwargs):
        raise AssertionError("this test does not call an LLM")
        yield


def _manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(_QuietLLM(), directory=tmp_path / "memory")


def _record(
    episode_id: str,
    goal: str,
    *,
    outcome: str = "",
    status: str = "completed",
    created_at: str = "2026-01-01T00:00:00Z",
    project: str = "alpha-proj",
    tools: tuple[dict, ...] = (),
    verification: tuple[dict, ...] = (),
    usage: int = 10,
    termination_reason: str = "",
    project_root: str = "/projects/alpha",
) -> EpisodeRecord:
    return EpisodeRecord(
        id=episode_id,
        session_id="session",
        goal=goal,
        status=status,  # type: ignore[arg-type]
        outcome=outcome,
        started_step=0,
        ended_step=1,
        created_at=created_at,
        plan={},
        tools=tools,
        agents=(),
        verification=verification,
        usage={
            "prompt_tokens": usage,
            "completion_tokens": 0,
            "total_tokens": usage,
        },
        version=2,
        project_id=project,
        project_root=project_root,
        root_run_id=episode_id.removeprefix("ep-"),
        termination_reason=termination_reason,
    )


def _open_turn(tmp_path: Path, prompt: str, *, project_root: Path | None = None) -> Session:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    session = Session.create(prompt, workspace, project_root=project_root)
    session.begin_user_turn(prompt)
    session.append_message({"role": "user", "content": prompt})
    return session


def _add_tool(session: Session, *, name: str = "bash", error: str = "") -> None:
    call = ToolCall(name, {}, "call_tool")
    session.record_assistant_turn("tool", {"route": "tool_calls"}, "tool_calls", [call])
    result = ToolResult.fail(error) if error else ToolResult.success({"ok": True})
    session.record_tool_execution("call_tool", result)


def _labeled_store(tmp_path: Path) -> tuple[EpisodeStore, dict[str, EpisodeRecord]]:
    """Fixed retrieval set: old experience, Chinese, English, failure, noise, other project."""
    store = EpisodeStore(tmp_path)
    labeled = {
        "old": _record(
            "ep-old-login",
            "retry the auth fixture after a login flake",
            outcome="cleared the stale session cookie",
            created_at="2024-01-01T00:00:00Z",
        ),
        "chinese": _record(
            "ep-zh-login",
            "修复登录测试",
            outcome="改了 session cookie 的过期时间",
            created_at="2025-06-01T00:00:00Z",
        ),
        "identifier": _record(
            "ep-get-user",
            "crash in getUserById when the cache is cold",
            outcome="split the camelCase lookup and returned the cached user",
            created_at="2025-07-01T00:00:00Z",
        ),
        "failure": _record(
            "ep-migration-lock",
            "apply the billing migration",
            outcome="",
            status="failed",
            created_at="2025-08-01T00:00:00Z",
            tools=({"name": "bash", "error": "migration lock timeout; release the advisory lock"},),
            termination_reason="failed",
        ),
        "noise": _record(
            "ep-color-recent",
            "更新配色",
            outcome="改成蓝色",
            created_at="2026-09-01T00:00:00Z",
        ),
        "other": _record(
            "ep-other-login",
            "修复登录测试",
            outcome="另一个项目的登录修复",
            created_at="2026-08-01T00:00:00Z",
            project="beta-proj",
        ),
    }
    for episode in labeled.values():
        store.save(episode)
    return store, labeled


def test_ingest_skips_empty_and_greeting_and_keeps_work(tmp_path: Path):
    manager = _manager(tmp_path)

    blank = _open_turn(tmp_path, "   ")
    blank.mark_completed()
    assert manager.finalize_turn(blank, None, extract_semantic=False)["episode_id"] is None

    greeting = _open_turn(tmp_path / "greet", "谢谢！")
    greeting.mark_completed()
    assert manager.finalize_turn(greeting, "不客气", extract_semantic=False)["episode_id"] is None

    recall_only = _open_turn(tmp_path / "recall", "placeholder")
    recall_only.message_records.clear()
    recall_only.active_turn_start_message_index = 0
    recall_only.append_message({
        "role": "user",
        "content": '<system-reminder source="wright-episode-recall">修复登录</system-reminder>',
    })
    recall_only.append_message({
        "role": "user",
        "content": "<hook-additional-context>修复登录</hook-additional-context>",
    })
    recall_only.append_message(
        {"role": "user", "content": "修复登录"}, source="runtime_event"
    )
    recall_only.mark_completed()
    assert manager.finalize_turn(recall_only, "done", extract_semantic=False)["episode_id"] is None

    answer = _open_turn(tmp_path / "answer", "修复登录测试")
    answer.mark_completed()
    saved_id = manager.finalize_turn(answer, "用 bun", extract_semantic=False)["episode_id"]
    saved = manager.episode_store.get(saved_id)
    assert saved.goal == "修复登录测试"
    assert saved.outcome == "用 bun"
    assert saved.version == 3
    assert saved.project_id == project_id(answer.project_root)

    failed = _open_turn(tmp_path / "failed", "跑迁移")
    _add_tool(failed, error="migration lock timeout")
    failed.mark_failed()
    failed_episode = manager.episode_store.get(
        manager.finalize_turn(failed, None, extract_semantic=False, termination_reason="failed")[
            "episode_id"
        ]
    )
    assert failed_episode.status == "failed"
    assert failed_episode.outcome == ""
    assert failed_episode.termination_reason == "failed"
    assert "migration lock timeout" in failed_episode.tools[0]["error"]

    cancelled = _open_turn(tmp_path / "cancel", "停下")
    _add_tool(cancelled, error="cancelled by user")
    cancelled.mark_cancelled()
    cancelled_episode = manager.episode_store.get(
        manager.finalize_turn(
            cancelled, None, extract_semantic=False, termination_reason="cancelled"
        )["episode_id"]
    )
    assert cancelled_episode.status == "cancelled"

    exhausted = _open_turn(tmp_path / "steps", "继续查")
    _add_tool(exhausted)
    exhausted.mark_max_steps()
    exhausted_episode = manager.episode_store.get(
        manager.finalize_turn(
            exhausted, None, extract_semantic=False, termination_reason="max_steps"
        )["episode_id"]
    )
    assert exhausted_episode.status == "failed"
    assert exhausted_episode.termination_reason == "max_steps"
    assert exhausted_episode.outcome == ""

    child = _open_turn(tmp_path / "child", "子任务")
    child.agent_task_id = "child-1"
    _add_tool(child)
    child.mark_completed()
    assert manager.finalize_turn(child, "done", extract_semantic=False)["episode_id"] is None
    assert "memory=" not in inspect.getsource(make_spawn_agent_tool)


def test_verification_failure_is_not_rendered_as_success(tmp_path: Path):
    manager = _manager(tmp_path)
    session = _open_turn(tmp_path, "核对登录测试")
    _add_tool(session)
    final = session.record_assistant_turn("未通过", {"route": "final"}, "final")
    session.record_verification(final, False, [{"code": "assert", "message": "login still fails"}])
    session.mark_completed()
    episode = manager.episode_store.get(
        manager.finalize_turn(session, "测试没有通过", extract_semantic=False)["episode_id"]
    )
    rendered = render_episode_for_budget(episode, token_limit=400)
    assert episode.status == "completed"
    assert rendered is not None
    assert "状态: completed" in rendered
    assert "approved=False" in rendered
    assert "login still fails" in rendered
    assert "测试已通过" not in rendered


def test_project_isolation_retention_and_legacy(tmp_path: Path):
    store = EpisodeStore(tmp_path)
    root = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    other = tmp_path / "other"
    for path in (root, worktree, other):
        path.mkdir()
    shared = Session.create("shared", worktree, project_root=root)
    shared.cwd = worktree / "nested"
    sibling = Session.create("sibling", root, project_root=root)
    sibling.cwd = Path("/tmp")
    foreign = Session.create("foreign", other)
    assert project_id(shared.project_root) == project_id(sibling.project_root)
    assert project_id(shared.project_root) != project_id(foreign.project_root)

    shared_id = project_id(root)
    for index in range(501):
        store.save(_record(
            f"ep-keep-{index:04d}",
            f"记录 {index}",
            created_at=f"2026-09-26T00:{index // 60:02d}:{index % 60:02d}Z",
            project=shared_id,
        ))
    store.save(_record(
        "ep-foreign-1",
        "修复登录测试",
        project=project_id(other),
        created_at="2026-09-26T03:00:00Z",
    ))
    kept = store.list(limit=500, project_id=shared_id)
    assert len(kept) == 500
    assert all(episode.project_id == shared_id for episode in kept)
    with pytest.raises(EpisodeStoreError):
        store.get("ep-keep-0000")
    assert store.get("ep-keep-0500").project_id == shared_id
    assert store.get("ep-foreign-1").project_id == project_id(other)

    legacy = {
        "id": "ep-legacy-old",
        "session_id": "old",
        "goal": "旧登录记录",
        "status": "max_steps",
        "outcome": "当时停在步数上限",
        "started_step": 0,
        "ended_step": 3,
        "created_at": "2023-01-01T00:00:00Z",
        "plan": {},
        "tools": [],
        "agents": [],
        "verification": [],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "ep-legacy-old.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    loaded = store.get("ep-legacy-old")
    assert loaded.version == 1
    assert loaded.project_id == ""
    assert loaded.status == "max_steps"
    legacy_hits = store.search("旧登录", scope="legacy")
    assert [hit.episode.id for hit in legacy_hits] == ["ep-legacy-old"]
    current_ids = {
        hit.episode.id
        for hit in store.search("登录", scope="current_project", project_id=shared_id, limit=100)
    }
    assert "ep-foreign-1" not in current_ids
    assert "ep-legacy-old" not in current_ids
    bare = _record("ep-no-project", "x", project="")
    with pytest.raises(EpisodeStoreError):
        store.save(bare)
    assert store.delete("ep-legacy-old").id == "ep-legacy-old"


def test_labeled_lexical_candidates_match_expectations(tmp_path: Path):
    store, _labeled = _labeled_store(tmp_path)
    for index in range(60):
        store.save(_record(
            f"ep-filler-{index:04d}",
            f"最近无关记录 {index}",
            created_at=f"2026-07-{(index % 28) + 1:02d}T00:{index % 60:02d}:00Z",
        ))

    chinese = store.search("登录测试", scope="current_project", project_id="alpha-proj", limit=40)
    chinese_ids = [hit.episode.id for hit in chinese]
    assert "ep-zh-login" in chinese_ids
    assert "ep-other-login" not in chinese_ids
    assert "ep-color-recent" not in chinese_ids
    assert chinese
    assert all(hit.lexical_score != 1.0 for hit in chinese)
    assert all(hit.lexical_score > 0 for hit in chinese)

    partial = store.search("登录 配色", scope="current_project", project_id="alpha-proj")
    assert any(hit.episode.id == "ep-zh-login" for hit in partial)

    english = store.search("LOGIN flake", scope="current_project", project_id="alpha-proj")
    assert english[0].episode.id == "ep-old-login"

    identifier = store.search("getUserById", scope="current_project", project_id="alpha-proj")
    assert identifier[0].episode.id == "ep-get-user"

    failure = store.search("advisory lock", scope="current_project", project_id="alpha-proj")
    assert failure[0].episode.id == "ep-migration-lock"
    assert failure[0].episode.status == "failed"

    old = store.search("zephyr", scope="current_project", project_id="alpha-proj", limit=5)
    assert old == []
    store.save(_record(
        "ep-zephyr-old",
        "zephyrquill rotation",
        outcome="rotated the zephyrquill",
        created_at="2020-01-01T00:00:00Z",
    ))
    buried = store.search("zephyrquill", scope="current_project", project_id="alpha-proj", limit=5)
    assert [hit.episode.id for hit in buried] == ["ep-zephyr-old"]

    empty = store.search("", scope="current_project", project_id="alpha-proj", limit=3)
    assert empty
    assert all(hit.lexical_score == 0.0 for hit in empty)

    usage_query = store.search("999999", scope="current_project", project_id="alpha-proj")
    assert usage_query == []

    same_goal = "shared billing incident"
    store.save(_record(
        "ep-fail-newer",
        same_goal,
        outcome="released the lock",
        status="failed",
        created_at="2026-09-02T00:00:00Z",
    ))
    store.save(_record(
        "ep-ok-older",
        same_goal,
        outcome="released the lock",
        status="completed",
        created_at="2026-09-01T00:00:00Z",
    ))
    ranked = store.search("shared billing incident", scope="current_project", project_id="alpha-proj")
    assert [hit.episode.id for hit in ranked[:2]] == ["ep-fail-newer", "ep-ok-older"]


def test_selector_protocol_and_single_usage(tmp_path: Path):
    assert "失败教训" in SELECT_SYSTEM_PROMPT
    assert "可以是 0 条" in SELECT_SYSTEM_PROMPT
    store = EpisodeStore(tmp_path)
    real = store.save(_record("ep-real-one", "修复登录", outcome="改了 cookie"))
    second = store.save(_record("ep-real-two", "修复登录", outcome="补了测试", created_at="2026-02-01T00:00:00Z"))
    third = store.save(_record("ep-real-three", "修复登录", outcome="回滚了", created_at="2026-03-01T00:00:00Z"))
    fourth = store.save(_record("ep-real-four", "修复登录", outcome="再试一次", created_at="2026-04-01T00:00:00Z"))

    class Streaming:
        def __call__(self, messages, **kwargs):
            yield ContentDelta(piece='{"selected_episodes": ["ep-ghost"], ')
            yield UsageEvent(UsageRecord(1, 1, 2))
            yield ContentDone(
                content=json.dumps({"selected_memories": [], "selected_episodes": [real.id]}),
                finish_reason="stop",
            )
            yield UsageEvent(UsageRecord(4, 5, 9))

    observed: list[UsageRecord] = []
    manager = MemoryManager(Streaming(), selector_llm=Streaming(), directory=tmp_path / "mem")
    manager.usage_observer = observed.append
    context = MemoryService(
        SemanticMemoryStore(tmp_path / "semantic"),
        store,
        selector=LlmContextSelector(manager._query),
    ).prepare_memory_context("登录", project_id="alpha-proj")
    assert context.selected_episode_ids == (real.id,)
    assert len(observed) == 1
    assert observed[0].total_tokens == 9

    def choose(payload: str, *, finish_reason: str | None = None):
        class Once:
            def __call__(self, messages, **kwargs):
                yield ContentDone(content=payload, finish_reason=finish_reason)

        return LlmContextSelector(Once()).select(
            task="登录", semantic_manifest="", episode_manifest=real.id
        )

    done_only = choose(json.dumps({"selected_episodes": [real.id]}))
    assert done_only.episode_ids == (real.id,)
    assert done_only.failed is False

    invalid = choose("not json")
    assert invalid.failed is True
    assert invalid.episodes_usable is False

    shape = choose("[1, 2]")
    assert shape.failure_type == "invalid_shape"

    wrong_type = choose(json.dumps({
        "selected_memories": ["note.md"],
        "selected_episodes": "ep-real-one",
    }))
    assert wrong_type.failed is False
    assert wrong_type.episodes_usable is False
    assert wrong_type.memory_ids == ("note.md",)

    both_bad = choose(json.dumps({"selected_memories": "nope", "selected_episodes": 3}))
    assert both_bad.failed is True

    class Boom:
        def __call__(self, messages, **kwargs):
            raise RuntimeError("selector down")

    exploded = LlmContextSelector(Boom()).select(task="t", semantic_manifest="", episode_manifest="")
    assert exploded.failed is True
    assert exploded.failure_type == "RuntimeError"

    unfinished = choose("{}", finish_reason="length")
    assert unfinished.failed is True

    class DeltaOnly:
        def __call__(self, messages, **kwargs):
            yield ContentDelta(piece="{}")

    missing_done = LlmContextSelector(DeltaOnly()).select(
        task="t", semantic_manifest="", episode_manifest=""
    )
    assert missing_done.failed is True

    class Picker:
        def select(self, *, task, semantic_manifest, episode_manifest):
            del task, semantic_manifest, episode_manifest
            return SelectorChoice(episode_ids=(
                "ep-unknown",
                real.id,
                real.id,
                second.id,
                third.id,
                fourth.id,
            ))

    picked = MemoryService(
        SemanticMemoryStore(tmp_path / "semantic-2"),
        store,
        selector=Picker(),
    ).prepare_memory_context("登录", project_id="alpha-proj")
    assert picked.selected_episode_ids == (real.id, second.id, third.id)

    class Broken:
        def select(self, *, task, semantic_manifest, episode_manifest):
            del task, semantic_manifest, episode_manifest
            return SelectorChoice(failed=True, episodes_usable=False, failure_type="invalid_shape")

    failed = MemoryService(
        SemanticMemoryStore(tmp_path / "semantic-3"),
        store,
        selector=Broken(),
    ).prepare_memory_context("登录", project_id="alpha-proj")
    assert failed.episodes == ()
    assert failed.episode_text == ""


def test_budget_ignores_historical_usage():
    small_usage = _record("ep-budget-same", "目标很短", outcome="结果很短", usage=1)
    huge_usage = _record("ep-budget-same", "目标很短", outcome="结果很短", usage=9_000_000)
    first = render_episode_for_budget(small_usage, token_limit=400)
    second = render_episode_for_budget(huge_usage, token_limit=400)
    assert first == second
    assert estimate_tokens(first or "") == estimate_tokens(second or "")
    assert "9000000" not in (first or "")


class _MemorylessSemantic:
    def list(self, limit=100):
        del limit
        return []

    def get(self, memory_id):
        raise AssertionError(memory_id)

    def search(self, query="", *, limit=20):
        del query, limit
        return []

    def save(self, *, name, description, type_, content):
        raise AssertionError(name)

    def delete(self, memory_id):
        raise AssertionError(memory_id)

    def read_index(self):
        return ""


class _MemorylessStore:
    def __init__(self, episodes: list[EpisodeRecord]):
        self.episodes = episodes

    def save(self, episode):
        return episode

    def get(self, episode_id):
        raise AssertionError(episode_id)

    def delete(self, episode_id):
        raise AssertionError(episode_id)

    def list(self, limit=100, *, project_id=None, scope=None):
        del project_id, scope
        return self.episodes[:limit]

    def search(self, query="", *, status=None, limit=20, scope="current_project", project_id=""):
        del query, status, scope, project_id
        from wright.domain.model.memory import EpisodeSearchHit
        return [
            EpisodeSearchHit(episode=episode, lexical_score=3.0)
            for episode in self.episodes[:limit]
        ]

    def recent(self, *, project_id, limit=10):
        del project_id
        return self.episodes[:limit]


class _ReturnAll:
    def select(self, *, task, semantic_manifest, episode_manifest):
        del task, semantic_manifest, episode_manifest
        return SelectorChoice(episode_ids=tuple(
            f"ep-budget-{index}" for index in range(1, 4)
        ))


def _policy(total: int, single: int):
    from wright.domain.policy.memory import EpisodePolicy
    return EpisodePolicy(
        max_episode_tokens_budget=total,
        max_single_episode_tokens=single,
    )


def test_text_budget_and_context_overflow(tmp_path: Path):
    long_goal = "登录失败需要看会话" * 400
    episodes = [
        _record(
            f"ep-budget-{index}",
            long_goal,
            outcome="结果正文" * 400,
            created_at=f"2026-01-0{index}T00:00:00Z",
        )
        for index in range(1, 4)
    ]
    forced = render_episode_for_budget(episodes[0], token_limit=80)
    assert forced is not None
    assert "…(已截断)" in forced
    assert estimate_tokens(forced) <= 80
    rendered = MemoryService(
        _MemorylessSemantic(),
        _MemorylessStore(episodes),
        episode_policy=_policy(500, 400),
        selector=_ReturnAll(),
    ).prepare_memory_context("登录", project_id="alpha-proj")
    assert rendered.episode_estimated_tokens <= 500
    assert estimate_tokens(rendered.episode_text) <= 500
    assert "ep-budget-1" in rendered.episode_text
    assert "ep-budget-2" not in rendered.episode_text
    assert "ep-budget-3" not in rendered.episode_text
    assert "…(已截断)" in rendered.episode_text

    zero = MemoryService(
        _MemorylessSemantic(),
        _MemorylessStore(episodes[:1]),
        episode_policy=_policy(0, 400),
        selector=_ReturnAll(),
    ).prepare_memory_context("登录", project_id="alpha-proj")
    assert zero.episode_text == ""
    assert zero.episode_estimated_tokens == 0

    tiny = MemoryService(
        _MemorylessSemantic(),
        _MemorylessStore(episodes[:1]),
        episode_policy=_policy(800, 8),
        selector=_ReturnAll(),
    ).prepare_memory_context("登录", project_id="alpha-proj")
    assert tiny.episode_text == ""

    english = _record("ep-budget-en", "fix login", outcome="cleared the cookie")
    chinese = _record("ep-budget-zh", "修复登录", outcome="清掉了 cookie")
    for episode in (english, chinese):
        text = render_episode_for_budget(episode, token_limit=400)
        assert text is not None
        assert estimate_tokens(text) <= 400

    user = MessageRecord("m-user", {"role": "user", "content": "请修复登录测试"}, "user_input")
    historical = MessageRecord(
        "m-old",
        {"role": "user", "content": '<system-reminder source="wright-episode-recall">\n旧经历\n</system-reminder>'},
        "user_input",
    )
    view = ContextBuilder(ContextCompactor()).build(
        [user, historical],
        tools=[],
        reminders=[{"role": "user", "content": "请按这个要求处理"}],
        optional_reminders=[{
            "role": "user",
            "content": '<system-reminder source="wright-episode-recall">\n' + ("经历" * 2000) + "\n</system-reminder>",
        }],
        context_limit=30,
        output_reserve_tokens=0,
    )
    assert view.omitted_optional_reminders is True
    projected = "\n".join(str(entry.message.get("content", "")) for entry in view.entries)
    assert "请修复登录测试" in projected
    assert "请按这个要求处理" in projected
    assert "经历经历" not in projected
    assert historical.message["content"].startswith("<system-reminder")
    assert all(entry.record_id != "m-old" for entry in view.entries)


def test_pending_lifecycle_checkpoint_and_concurrency(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO)
    manager = _manager(tmp_path)
    session = _open_turn(tmp_path, "修复登录失败")
    task = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="spawn_1",
        depth=1,
        task="inspect auth",
        requested_steps=3,
    )
    session.mark_completed()
    deferred = manager.finalize_turn(session, "先给出方向", extract_semantic=False)
    assert deferred["pending"] is True
    assert deferred["episode_id"] is None
    assert manager.episode_store.list() == []
    manager.recover_pending(session)
    assert session.pending_episode_finalizes
    assert manager.episode_store.list() == []

    session.control_plane.finish_task(
        task.id, status="completed", steps_used=1, result="child completed"
    )
    settled = manager.finalize_turn(session, "补充了 cookie 修复", extract_semantic=False)
    episode = manager.episode_store.get(settled["episode_id"])
    assert "先给出方向" in episode.outcome
    assert "补充了 cookie 修复" in episode.outcome
    assert episode.agents[0]["status"] == "completed"
    repeated = manager.finalize_turn(session, "不应该覆盖", extract_semantic=False)
    assert repeated["episode_id"] == episode.id
    assert manager.episode_store.get(episode.id).outcome == episode.outcome
    assert len(manager.episode_store.list()) == 1

    cross = _open_turn(tmp_path / "cross", "旧目标：修登录")
    old_task = cross.control_plane.begin_task(
        root_turn_id=cross.agent_root_turn_id,
        parent_id=None,
        tool_call_id="spawn_old",
        depth=1,
        task="read the old trace",
        requested_steps=2,
    )
    old_turn = cross.agent_root_turn_id
    cross.mark_completed()
    assert manager.finalize_turn(cross, "旧回答", extract_semantic=False)["pending"] is True
    cross.begin_user_turn("把按钮改成蓝色")
    cross.append_message({"role": "user", "content": "把按钮改成蓝色"})
    cross.plan_manager.create_plan("paint", ["blue"])
    _add_tool(cross, name="paint")
    cross.control_plane.finish_task(old_task.id, status="failed", steps_used=1, error="child failed")
    saved = manager.settle_previous_turn(
        cross,
        root_turn_id=old_turn,
        task={"id": old_task.id, "status": "failed", "error": "child failed"},
    )
    old_episode = manager.episode_store.get(saved["episode_id"])
    assert old_episode.goal == "旧目标：修登录"
    assert "蓝色" not in old_episode.goal
    assert "paint" not in json.dumps(old_episode.tools, ensure_ascii=False)
    assert old_episode.agents[0]["status"] == "failed"
    again = manager.settle_previous_turn(cross, root_turn_id=old_turn, task={"id": old_task.id})
    assert again["episode_id"] is None
    assert len([item for item in manager.episode_store.list(limit=20) if item.id == old_episode.id]) == 1

    waiting = _open_turn(tmp_path / "wait", "等待后台")
    live = waiting.control_plane.begin_task(
        root_turn_id=waiting.agent_root_turn_id,
        parent_id=None,
        tool_call_id="spawn_live",
        depth=1,
        task="still running",
        requested_steps=2,
    )
    waiting.mark_completed()
    manager.finalize_turn(waiting, "先挂起", extract_semantic=False)
    repo = FileSessionRepository(tmp_path / "checkpoints")
    repo.save(waiting)
    loaded = repo.load(waiting.session_id)
    assert loaded.pending_episode_finalizes
    restored_task = loaded.control_plane.tree(loaded.agent_root_turn_id)[0]
    assert restored_task["status"] == "failed"
    assert "interrupted" in str(restored_task.get("error") or "")
    manager.recover_pending(loaded)
    assert loaded.pending_episode_finalizes == []
    recovered = manager.episode_store.list(limit=50)
    assert any(item.goal == "等待后台" for item in recovered)

    missing = FileSessionRepository(tmp_path / "old-checkpoints")
    bare = _open_turn(tmp_path / "bare", "没有待收口")
    bare.mark_completed()
    path = missing.save(bare)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("pending_episode_finalizes", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert missing.load(bare.session_id).pending_episode_finalizes == []

    fragile = _open_turn(tmp_path / "disk", "磁盘失败时保留快照")
    _add_tool(fragile)
    fragile.mark_completed()
    original = manager.episode_store.save

    def fail_save(episode):
        raise OSError("disk")

    manager.episode_store.save = fail_save  # type: ignore[method-assign]
    failed_save = manager.finalize_turn(fragile, "已经答完", extract_semantic=False)
    assert failed_save["episode_id"] is None
    assert fragile.pending_episode_finalizes
    manager.episode_store.save = original  # type: ignore[method-assign]
    manager.recover_pending(fragile)
    assert fragile.pending_episode_finalizes == []
    assert any(item.goal == "磁盘失败时保留快照" for item in manager.episode_store.list(limit=50))

    for status in ("failed", "cancelled"):
        extra = _open_turn(tmp_path / status, f"后台{status}")
        extra_task = extra.control_plane.begin_task(
            root_turn_id=extra.agent_root_turn_id,
            parent_id=None,
            tool_call_id=f"spawn_{status}",
            depth=1,
            task=f"child {status}",
            requested_steps=2,
        )
        extra.mark_completed()
        assert manager.finalize_turn(extra, "等待子任务", extract_semantic=False)["pending"] is True
        extra.control_plane.finish_task(
            extra_task.id, status=status, steps_used=1, error=f"child {status}"
        )
        finished = manager.finalize_turn(extra, "子任务结束", extract_semantic=False)
        assert manager.episode_store.get(finished["episode_id"]).agents[0]["status"] == status

    raced: list[str] = []
    race_store = EpisodeStore(tmp_path / "race")

    def save_one(index: int) -> None:
        saved = race_store.save(_record(
            f"ep-race-{index:04d}",
            f"并发 {index}",
            created_at=f"2026-05-01T00:{index // 60:02d}:{index % 60:02d}Z",
            project="race-proj",
        ))
        raced.append(saved.id)

    threads = [threading.Thread(target=save_one, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(raced)) == 40
    assert len(race_store.list(limit=50, project_id="race-proj")) == 40
    assert "磁盘失败时保留快照" not in caplog.text
    assert "episode_persist_failed" in caplog.text
    assert live.id


def test_agent_injects_episode_without_writing_transcript(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session.create("t", workspace)
    memory_dir = tmp_path / "memory"
    seeded_store = EpisodeStore(memory_dir)
    seeded = seeded_store.save(_record(
        "ep-seed-login",
        "修复登录测试",
        outcome="改用 session cookie",
        project=project_id(session.project_root),
        project_root=str(session.project_root),
    ))

    class Main:
        context_limit = 128000
        def __init__(self):
            self.requests = []

        def __call__(self, messages, **kwargs):
            self.requests.append(list(messages))
            yield event(content=response(content="done", calls=[]))

    class Selector:
        def __init__(self):
            self.recall_calls = 0

        def __call__(self, messages, **kwargs):
            text = json.dumps(messages, ensure_ascii=False)
            if "<recall-data>" in text:
                self.recall_calls += 1
                payload = {"selected_memories": [], "selected_episodes": [seeded.id]}
            else:
                payload = {"memories": []}
            yield event(content=json.dumps(payload, ensure_ascii=False))

    main = Main()
    selector = Selector()
    manager = MemoryManager(main, selector_llm=selector, directory=memory_dir)
    agent = create_agent(main, [], session, SilentRenderer(), memory=manager)

    assert agent.run("登录又失败了") == "done"
    projected = [
        message
        for request in main.requests
        for message in request
        if "wright-episode-recall" in str(message.get("content", ""))
    ]
    assert projected
    assert seeded.id in projected[0]["content"]
    assert "改用 session cookie" in projected[0]["content"]
    assert not any(
        "wright-episode-recall" in str(record.message.get("content", ""))
        for record in session.message_records
    )
    after_first = selector.recall_calls
    assert after_first == 1

    agent.run_runtime_event({
        "type": "task_notification",
        "task": {
            "id": "not-a-task",
            "root_turn_id": session.agent_root_turn_id,
            "status": "completed",
        },
    })
    assert selector.recall_calls == after_first

    assert agent.run("再看一个问题") == "done"
    assert selector.recall_calls == after_first + 1
    assert manager.episode_store.get(seeded.id).outcome == "改用 session cookie"


def test_tool_executor_search_scopes(tmp_path: Path):
    store = EpisodeStore(tmp_path)
    current = store.save(_record("ep-current-login", "修复登录", outcome="当前项目"))
    other = store.save(_record(
        "ep-cross-login", "修复登录", outcome="其他项目", project="beta-proj"
    ))
    legacy_path = store.directory / "ep-legacy-login.json"
    legacy_path.write_text(json.dumps({
        "id": "ep-legacy-login",
        "session_id": "old",
        "goal": "旧的登录记录",
        "status": "completed",
        "outcome": "旧结果",
        "started_step": 0,
        "ended_step": 1,
        "created_at": "2022-01-01T00:00:00Z",
        "plan": {},
        "tools": [],
        "agents": [],
        "verification": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }), encoding="utf-8")
    service = MemoryService(SemanticMemoryStore(tmp_path / "semantic"), store)
    tools = {tool.name: tool for tool in build_episode_tools(service, project_id="alpha-proj")}
    delete_access = tools["delete_episode"].describe_access({"episode_id": current.id})
    assert "persistent_write" in delete_access.operations
    assert "deletes_data" in delete_access.risk_flags

    def execute(name: str, arguments: dict):
        prepared = SimpleNamespace(
            tool=tools[name],
            call=ToolCall(name, arguments, f"call-{name}"),
            local_cancel=threading.Event(),
            effective_timeout=5,
            approval_wait_ms=0,
            final_arguments=arguments,
            runtime=ToolRuntime(tool_name=name),
            resolution=SimpleNamespace(decision="allow", reason="test", source="test"),
            execution=None,
        )
        outcome = ConcurrentToolExecutor(journal=None).execute_batch([(0, prepared)])[0]
        assert outcome.result.ok, outcome.result.err
        return outcome.result.data

    current_hits = execute("search_episodes", {"query": "登录"})
    assert [item["id"] for item in current_hits["episodes"]] == [current.id]
    assert current_hits["episodes"][0]["project_source"] == "alpha-proj"
    assert "lexical_score" in current_hits["episodes"][0]

    everything = execute("search_episodes", {"query": "登录", "scope": "all_projects"})
    assert {item["id"] for item in everything["episodes"]} == {current.id, other.id}

    legacy_hits = execute("search_episodes", {"query": "旧的登录", "scope": "legacy"})
    assert [item["id"] for item in legacy_hits["episodes"]] == ["ep-legacy-login"]
    assert legacy_hits["episodes"][0]["project_source"] == "legacy"

    loaded = execute("get_episode", {"episode_id": "ep-legacy-login"})
    assert loaded["project_source"] == "legacy"
    assert loaded["status"] == "completed"
    deleted = execute("delete_episode", {"episode_id": "ep-legacy-login"})
    assert deleted["id"] == "ep-legacy-login"
    missing, error = service.get_episode("ep-legacy-login")
    assert missing is None
    assert error
