import pytest

from wright.application.memory import MemoryManager
from wright.application.memory.episode import episode_from_session
from wright.application.memory.memory_service import MemoryService
from wright.domain.gateway.memory import SelectorChoice
from wright.domain.model.session import Session, UsageRecord
from wright.domain.model.tool import ToolCall, ToolResult
from wright.infrastructure.persistence.memory import (
    EpisodeNotFoundError,
    EpisodeStore,
    EpisodeStoreError,
    SemanticMemoryStore,
)
from wright.infrastructure.tools.memory import build_episode_tools


def _completed_session(tmp_path):
    session = Session.create("placeholder", tmp_path)
    session.begin_user_turn("修复登录测试")
    session.append_message({"role": "user", "content": "修复登录测试"})
    session.plan_manager.create_plan("修复登录", ["修改实现"])
    session.plan_manager.update_step("step_1", "completed", note="tests pass")

    call = ToolCall(
        "execute_command",
        {"command": "pytest", "token": "secret-must-not-be-persisted"},
        "call_1",
    )
    turn = session.record_assistant_turn(
        "tool call", {"tool_calls": []}, "tool_calls", [call]
    )
    session.record_usage_for_turn(turn, UsageRecord(10, 5, 15))
    session.record_tool_execution(
        "call_1",
        ToolResult.success({"stdout": "secret-result-must-not-be-persisted"}),
    )
    final_turn = session.record_assistant_turn(
        "已修复",
        {"tool_calls": [], "final_answer": "已修复"},
        "final",
    )
    session.record_usage_for_turn(final_turn, UsageRecord(8, 3, 11))
    session.record_verification(final_turn, True, [])
    session.mark_completed()
    return session


def test_episode_is_compact_sanitized_and_idempotent(tmp_path):
    session = _completed_session(tmp_path)
    store = EpisodeStore(tmp_path)
    episode = episode_from_session(session, "已修复")

    first = store.save(episode)
    second = store.save(episode_from_session(session, "重复 finalize"))

    assert first.id == second.id
    assert len(store.list()) == 1
    assert first.tools == ({
        "step": 1,
        "name": "execute_command",
        "status": "succeeded",
        "ok": True,
        "error": "",
    },)
    raw = store.path_for(first.id).read_text(encoding="utf-8")
    assert "secret-must-not-be-persisted" not in raw
    assert "secret-result-must-not-be-persisted" not in raw
    assert first.usage == {"prompt_tokens": 18, "completion_tokens": 8, "total_tokens": 26}
    assert store.path_for(first.id).stat().st_mode & 0o777 == 0o600


def test_episode_id_distinguishes_turns_cancelled_before_first_step(tmp_path):
    session = Session.create("placeholder", tmp_path)
    session.begin_user_turn("same goal")
    session.append_message({"role": "user", "content": "same goal"})
    session.mark_failed()
    first = episode_from_session(session, None)

    session.begin_user_turn("follow-up")
    session.begin_user_turn("same goal")
    session.append_message({"role": "user", "content": "same goal"})
    session.mark_failed()
    second = episode_from_session(session, None)

    assert first.started_step == second.started_step == 0
    assert first.id != second.id


def test_episode_store_search_get_delete_and_validation(tmp_path):
    store = EpisodeStore(tmp_path)
    episode = store.save(episode_from_session(_completed_session(tmp_path), "已修复登录"))

    assert store.get(episode.id) == episode
    hits = store.search(
        "登录", status="completed", scope="current_project", project_id=episode.project_id
    )
    assert [hit.episode for hit in hits] == [episode]
    assert hits[0].lexical_score > 0
    assert hits[0].lexical_score != 1.0
    assert store.search(
        "没有匹配", scope="current_project", project_id=episode.project_id
    ) == []
    with pytest.raises(EpisodeStoreError, match="status"):
        store.search(status="unknown")  # type: ignore[arg-type]
    with pytest.raises(EpisodeStoreError, match="limit"):
        store.search(limit=0)

    assert store.delete(episode.id) == episode
    with pytest.raises(EpisodeNotFoundError):
        store.get(episode.id)


class _EpisodeSelector:
    def __init__(self, episode_id):
        self.episode_id = episode_id

    def select(self, *, task, semantic_manifest, episode_manifest):
        del task, semantic_manifest, episode_manifest
        return SelectorChoice(episode_ids=(self.episode_id,))


def test_episode_recall_is_marked_as_historical_not_current_evidence(tmp_path):
    store = EpisodeStore(tmp_path)
    episode = store.save(episode_from_session(_completed_session(tmp_path), "已修复"))
    service = MemoryService(
        SemanticMemoryStore(tmp_path),
        store,
        selector=_EpisodeSelector(episode.id),
    )

    context = service.prepare_memory_context(
        "登录测试怎么修", project_id=episode.project_id
    )

    assert episode.id in context.episode_text
    assert "历史执行经历" in context.episode_text
    assert "不表示测试已经通过" in context.episode_text
    assert "测试已通过" not in context.episode_text


def test_episode_tools_are_read_only_except_permissioned_forget(tmp_path):
    service = MemoryService(SemanticMemoryStore(tmp_path), EpisodeStore(tmp_path))
    tools = build_episode_tools(service, project_id="demo")
    assert [tool.name for tool in tools] == [
        "search_episodes", "get_episode", "delete_episode"
    ]
    assert tools[-1].describe_access({"episode_id": "ep"}).operations == frozenset({"persistent_write"})


def test_episode_captures_compact_subagent_execution_summary(tmp_path):
    session = _completed_session(tmp_path)
    task = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="spawn_1",
        depth=1,
        task="inspect auth",
        requested_steps=5,
    )
    session.control_plane.finish_task(
        task.id,
        status="completed",
        steps_used=2,
        result="auth finding",
    )

    episode = episode_from_session(session, "已修复")

    assert episode.agents == ({
        "id": task.id,
        "parent_id": None,
        "depth": 1,
        "task": "inspect auth",
        "status": "completed",
        "steps_used": 2,
        "total_tokens": 0,
        "result": "auth finding",
        "error": "",
    },)


class _UnusedLLM:
    def __call__(self, messages, **kwargs):
        raise AssertionError("episode gate must not call the model")


def test_record_episode_skips_empty_turns_and_keeps_traces(tmp_path):
    manager = MemoryManager(_UnusedLLM(), directory=tmp_path)

    empty = Session.create("placeholder", tmp_path)
    empty.begin_user_turn("hi")
    empty.append_message({"role": "user", "content": "hi"})
    empty.mark_cancelled()
    assert manager.record_episode(empty, None) is None
    assert manager.episode_store.list() == []

    answered = Session.create("placeholder", tmp_path)
    answered.begin_user_turn("我该用什么包管理器")
    answered.append_message({"role": "user", "content": "我该用什么包管理器"})
    answered.mark_completed()
    saved_answer = manager.record_episode(answered, "用 bun")
    assert saved_answer is not None
    assert saved_answer.tools == ()
    assert saved_answer.outcome == "用 bun"

    failed = Session.create("placeholder", tmp_path)
    failed.begin_user_turn("修复登录测试")
    failed.append_message({"role": "user", "content": "修复登录测试"})
    call = ToolCall("execute_command", {"command": "pytest"}, "call_1")
    failed.record_assistant_turn("tool call", {"tool_calls": []}, "tool_calls", [call])
    failed.record_tool_execution("call_1", ToolResult.fail("assertion failed"))
    failed.mark_failed()
    saved_failure = manager.record_episode(failed, None)
    assert saved_failure is not None
    assert saved_failure.status == "failed"
    assert saved_failure.tools[0]["ok"] is False
    assert manager.episode_store.get(saved_failure.id).status == "failed"

    cancelled = Session.create("placeholder", tmp_path)
    cancelled.begin_user_turn("修复登录测试")
    cancelled.append_message({"role": "user", "content": "修复登录测试"})
    cancel_call = ToolCall("execute_command", {"command": "pytest"}, "call_1")
    cancelled.record_assistant_turn(
        "tool call", {"tool_calls": []}, "tool_calls", [cancel_call]
    )
    cancelled.record_tool_execution("call_1", ToolResult.fail("cancelled"))
    cancelled.mark_cancelled()
    saved_cancel = manager.record_episode(cancelled, None)
    assert saved_cancel is not None
    assert saved_cancel.status == "cancelled"
    assert manager.episode_store.get(saved_cancel.id).status == "cancelled"
