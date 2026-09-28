"""端到端:验证 Agent 在一轮 run 中注入召回、收口后触发提取。

用假 LLM 串起整条主循环——不打真实网络。
"""

import json
import re
from pathlib import Path

from tests.responses import event, response
from wright.application.agent import create_agent
from wright.application.memory.assembly import assemble_memory_manager
from wright.domain.model.llm.events import UsageEvent
from wright.domain.model.session import Session
from wright.infrastructure.persistence.memory import write_memory_file


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class MainLLM:
    """主对话 LLM:每轮直接给出 final_answer,让 run() 一轮收口。"""

    context_limit = 128000

    def __init__(self):
        self.requests = []

    def __call__(self, messages, **kwargs):
        self.requests.append(list(messages))
        yield UsageEvent(_Usage())
        yield event(
            content=response(content="done", calls=[]),
            reasoning="",
        )


class SelectorLLM:
    """召回/提取共用的 side-query LLM:同时带两套键,各取所需。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        yield UsageEvent(_Usage())
        text = str(messages[-1].get("content") if messages else "")
        if "kind=user_statement" in text:
            found = re.search(r"\[(ev-[A-Za-z0-9_-]+)", text)
            payload = {
                "memories": [{
                    "name": "session-fact",
                    "description": "extracted in session",
                    "type": "user",
                    "content": "以后只用 bun",
                    "action": "create",
                    "source_refs": [found.group(1)] if found else [],
                }]
            }
        else:
            payload = {
                "selected_memories": ["user-likes-bun.md"],
                "selected_episodes": [],
            }
        yield event(content=json.dumps(payload, ensure_ascii=False), reasoning="")


class EmptySelectorLLM:
    def __call__(self, messages, **kwargs):
        yield event(content=json.dumps({
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
    main = MainLLM()
    manager = assemble_memory_manager(main, selector_llm=selector, directory=tmp_path)
    session = Session.create(initial_goal="t", workspace_dir=tmp_path)
    agent = create_agent(main, [], session, memory=manager)

    answer = agent.run("记住我以后只用 bun")
    assert answer == "done"
    assert session.total_usage.total_tokens == 15 * (1 + selector.calls)
    assert session.last_usage.total_tokens == 15
    assert session.request_context_tokens > 0  # side-query usage does not replace local estimate

    # 1) system prompt 含静态记忆指令段
    sys_msg = session.message_records[0].message
    assert sys_msg["role"] == "system"
    assert "长期记忆" in sys_msg["content"]

    # 2) 召回块只出现在本次请求投影，不写入历史 transcript
    projected = [
        message
        for request in main.requests
        for message in request
        if message.get("role") == "user" and "<system-reminder" in str(message.get("content", ""))
    ]
    assert projected, "召回块未注入"
    assert "用 bun" in projected[0]["content"]
    assert "wright-semantic-recall" in projected[0]["content"]
    assert not any(
        "<system-reminder" in str(record.message.get("content", ""))
        for record in session.message_records
    )

    # 3) 收口后提取落盘了新记忆 + 重建了索引。身份不是标题。
    bodies = [path.read_text(encoding="utf-8") for path in tmp_path.glob("mem-*.md")]
    assert any("以后只用 bun" in text for text in bodies)
    assert "session-fact" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

    # 4) 同一个 user turn 自动形成独立 episode；语义扫描不会把它当成 markdown 记忆
    episodes = manager.episode_store.list()
    assert len(episodes) == 1
    assert episodes[0].goal == "记住我以后只用 bun"
    assert episodes[0].status == "completed"
    assert episodes[0].outcome == "done"


def test_greeting_skips_semantic_extract_and_episode(tmp_path: Path):
    selector = SelectorLLM()
    manager = assemble_memory_manager(MainLLM(), selector_llm=selector, directory=tmp_path)
    session = Session.create(initial_goal="t", workspace_dir=tmp_path)
    agent = create_agent(MainLLM(), [], session, memory=manager)

    assert agent.run("hi") == "done"
    assert selector.calls == 0
    assert not (tmp_path / "session-fact.md").exists()
    assert manager.episode_store.list() == []


def test_agent_without_memory_unaffected(tmp_path: Path):
    """memory=None 时:无记忆段、无召回注入,行为与原 Agent 一致。"""
    session = Session.create(initial_goal="t", workspace_dir=tmp_path)
    agent = create_agent(MainLLM(), [], session)  # 不传 memory
    answer = agent.run("hi")
    assert answer == "done"
    wire = [r.message for r in session.message_records]
    assert "长期记忆" not in wire[0]["content"]
    assert not any("<system-reminder>" in str(m.get("content", "")) for m in wire)


def test_new_user_turn_resets_plan_and_episode_does_not_inherit_old_plan(tmp_path: Path):
    manager = assemble_memory_manager(MainLLM(), selector_llm=EmptySelectorLLM(), directory=tmp_path)
    session = Session.create(initial_goal="t", workspace_dir=tmp_path)
    session.plan_manager.create_plan("first task", ["finish first"])
    session.plan_manager.update_step("step_1", "completed")
    agent = create_agent(MainLLM(), [], session, memory=manager)

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
        def __call__(self, messages, **kwargs):
            raise RuntimeError("selector unavailable")

    manager = assemble_memory_manager(MainLLM(), selector_llm=BrokenSelector(), directory=tmp_path)
    block = manager.recall_block("query")
    assert "forces selector call" in block
    assert "Traceback" not in block
