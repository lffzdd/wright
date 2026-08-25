"""端到端:验证 Agent 在一轮 run 中注入召回、收口后触发提取。

用假 LLM 串起整条主循环——不打真实网络。
"""

import json
from pathlib import Path

from ...agent import Agent
from ...events import ContentDone, UsageEvent
from ...memory import MemoryManager
from ...memory.store import write_memory_file
from ...renderer import SilentRenderer
from ...session import SessionState


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class MainLLM:
    """主对话 LLM:每轮直接给出 final_answer,让 run() 一轮收口。"""

    context_limit = 128000

    def __call__(self, messages):
        yield UsageEvent(_Usage())
        yield ContentDone(
            content=json.dumps({"tool_calls": [], "final_answer": "done"}),
            reasoning="",
        )


class SelectorLLM:
    """召回/提取共用的 side-query LLM:同时带两套键,各取所需。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        payload = {
            "selected_memories": ["user-likes-bun.md"],  # recall 取这个
            "memories": [  # extract 取这个
                {
                    "name": "session-fact",
                    "description": "extracted in session",
                    "type": "project",
                    "content": "事实正文",
                    "action": "create",
                }
            ],
        }
        yield ContentDone(content=json.dumps(payload, ensure_ascii=False), reasoning="")


class EmptySelectorLLM:
    def __call__(self, messages):
        yield ContentDone(content=json.dumps({
            "selected_memories": [],
            "selected_episodes": [],
            "memories": [],
        }), reasoning="")


def test_agent_recall_injection_and_extraction(tmp_path: Path):
    # 预置一条记忆,供召回选中
    write_memory_file(
        "user-likes-bun", "prefers bun over npm", "feedback", "用 bun", directory=tmp_path
    )

    selector = SelectorLLM()
    manager = MemoryManager(MainLLM(), selector_llm=selector, directory=tmp_path)
    session = SessionState.create(user_goal="t", workspace_dir=tmp_path)
    agent = Agent(MainLLM(), [], session, SilentRenderer(), memory=manager)

    answer = agent.run("我该用什么包管理器")
    assert answer == "done"

    # 1) system prompt 含静态记忆指令段
    sys_msg = session.message_records[0].message
    assert sys_msg["role"] == "system"
    assert "长期记忆" in sys_msg["content"]

    # 2) 召回块作为 system-reminder 被注入(用户消息之后)
    wire = [r.message for r in session.message_records]
    reminders = [
        m for m in wire
        if m["role"] == "user" and "<system-reminder>" in str(m.get("content", ""))
    ]
    assert reminders, "召回块未注入"
    assert "用 bun" in reminders[0]["content"]

    # 3) 收口后提取落盘了新记忆 + 重建了索引
    assert (tmp_path / "session-fact.md").is_file()
    assert "session-fact" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    # 4) 同一个 user turn 自动形成独立 episode；语义扫描不会把它当成 markdown 记忆
    episodes = manager.episode_store.list()
    assert len(episodes) == 1
    assert episodes[0].goal == "我该用什么包管理器"
    assert episodes[0].status == "completed"
    assert episodes[0].outcome == "done"


def test_agent_without_memory_unaffected(tmp_path: Path):
    """memory=None 时:无记忆段、无召回注入,行为与原 Agent 一致。"""
    session = SessionState.create(user_goal="t", workspace_dir=tmp_path)
    agent = Agent(MainLLM(), [], session, SilentRenderer())  # 不传 memory
    answer = agent.run("hi")
    assert answer == "done"
    wire = [r.message for r in session.message_records]
    assert "长期记忆" not in wire[0]["content"]
    assert not any("<system-reminder>" in str(m.get("content", "")) for m in wire)


def test_new_user_turn_resets_plan_and_episode_does_not_inherit_old_plan(tmp_path: Path):
    manager = MemoryManager(MainLLM(), selector_llm=EmptySelectorLLM(), directory=tmp_path)
    session = SessionState.create(user_goal="t", workspace_dir=tmp_path)
    session.plan_manager.create_plan("first task", ["finish first"])
    session.plan_manager.update_step("step_1", "completed")
    agent = Agent(MainLLM(), [], session, SilentRenderer(), memory=manager)

    assert agent.run("first") == "done"
    assert agent.run("second") == "done"

    episodes = sorted(manager.episode_store.list(), key=lambda item: item.started_step)
    assert len(episodes) == 2
    assert episodes[0].plan["objective"] == "first task"
    assert episodes[1].plan["status"] == "empty"
    assert episodes[1].plan["steps"] == []


def test_recall_failure_is_best_effort(tmp_path: Path):
    write_memory_file("context", "forces selector call", "project", "x", directory=tmp_path)

    class BrokenSelector:
        def __call__(self, messages):
            raise RuntimeError("selector unavailable")

    manager = MemoryManager(MainLLM(), selector_llm=BrokenSelector(), directory=tmp_path)
    assert manager.recall_block("query") == ""
