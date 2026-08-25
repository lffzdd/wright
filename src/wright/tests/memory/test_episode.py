import json

import pytest

from ...events import ContentDone
from ...memory.episode import (
    EpisodeNotFoundError,
    EpisodeStore,
    EpisodeStoreError,
    episode_from_session,
)
from ...memory.recall import build_recall_block
from ...session import SessionState, UsageRecord
from ...tools.base import ToolCall, ToolResult
from ...tools.episode_tools import build_episode_tools


def _completed_session(tmp_path):
    session = SessionState.create("placeholder", tmp_path)
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
        json.dumps({"tool_calls": [], "final_answer": "已修复"}),
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
    session = SessionState.create("placeholder", tmp_path)
    session.begin_user_turn("same goal")
    session.append_message({"role": "user", "content": "same goal"})
    session.mark_failed()
    first = episode_from_session(session, None)

    session.mark_running()
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
    assert store.search("登录", status="completed") == [episode]
    assert store.search("没有匹配") == []
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

    def __call__(self, messages):
        yield ContentDone(json.dumps({
            "selected_memories": [],
            "selected_episodes": [self.episode_id],
        }, ensure_ascii=False))


def test_episode_recall_is_marked_as_historical_not_current_evidence(tmp_path):
    store = EpisodeStore(tmp_path)
    episode = store.save(episode_from_session(_completed_session(tmp_path), "已修复"))

    block = build_recall_block("登录测试怎么修", _EpisodeSelector(episode.id), tmp_path)

    assert episode.id in block
    assert "历史执行经历" in block
    assert "不代表当前文件、测试或外部状态" in block


def test_episode_tools_are_read_only_except_permissioned_forget(tmp_path):
    tools = build_episode_tools(tmp_path)
    assert [tool.name for tool in tools] == [
        "search_episodes", "get_episode", "delete_episode"
    ]
    assert tools[-1].check_permission is not None


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
