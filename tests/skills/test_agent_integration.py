"""Skill disclosure follows the production loader.

These tests capture scripted model requests. They do not call a live model.
"""

from pathlib import Path

from tests.responses import event, response
from wright.application.agent import create_agent
from wright.application.skills import SkillRegistry
from wright.domain.model.session import Session
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.storage.skills import write_skill
from wright.infrastructure.tools.skill_tools import resident_skill_tools


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages = []
        self.seen_tools = []

    def __call__(self, messages, **kwargs):
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(kwargs.get("tools") or []))
        yield event(content=self.script.pop(0))


def _tool(name, **arguments):
    return response(content=None, calls=[{"name": name, "arguments": arguments}])


def _final(answer):
    return response(content=answer, calls=[])


def _write_skill(directory: Path) -> SkillRegistry:
    write_skill(
        directory,
        "release-check",
        name="release-check",
        description="发布时使用的检查流程",
        body="发布前必须先跑测试。",
        allowed_tools=["execute_command"],
    )
    return SkillRegistry(directory)


def _catalog_texts(messages) -> list[str]:
    return [
        str(message.get("content", ""))
        for message in messages
        if "<skill-catalog>" in str(message.get("content", ""))
    ]


def _schema_names(tools) -> list[str]:
    return [item["name"] for item in tools]


def test_empty_registry_exposes_neither_catalog_nor_loader(tmp_path: Path):
    llm = ScriptLLM([_final("done")])
    session = Session.create("task", tmp_path)
    registry = SkillRegistry(tmp_path)
    create_agent(
        llm, resident_skill_tools(registry), session, skills=registry,
    ).run("hi")
    assert not any(_catalog_texts(batch) for batch in llm.seen_messages)
    assert all("load_skill" not in _schema_names(tools) for tools in llm.seen_tools)
    assert not any(
        "<skill-catalog>" in str(record.message.get("content", ""))
        for record in session.message_records
    )


def test_catalog_is_projected_each_request_and_not_appended(tmp_path: Path):
    registry = _write_skill(tmp_path)
    llm = ScriptLLM([
        _tool("load_skill", skill_id="release-check"),
        _final("已按流程检查"),
    ])
    session = Session.create("task", tmp_path)
    answer = create_agent(
        llm, resident_skill_tools(registry), session, skills=registry,
    ).run("准备发布")

    assert answer == "已按流程检查"
    first_catalogs = _catalog_texts(llm.seen_messages[0])
    assert len(first_catalogs) == 1
    assert "release-check" in first_catalogs[0]
    assert "发布时使用的检查流程" in first_catalogs[0]
    assert "load_skill" in first_catalogs[0]
    assert "load_skill" in _schema_names(llm.seen_tools[0])

    second_catalogs = _catalog_texts(llm.seen_messages[1])
    assert len(second_catalogs) == 1
    assert "发布前必须先跑测试" in str(llm.seen_messages[1])
    transcript = [
        str(record.message.get("content", ""))
        for record in session.message_records
    ]
    assert sum("<skill-catalog>" in text for text in transcript) == 0
    assert any("发布前必须先跑测试" in text for text in transcript)


def test_later_user_turn_refreshes_catalog_without_duplicating_history(tmp_path: Path):
    registry = _write_skill(tmp_path)
    llm = ScriptLLM([
        _final("第一轮完成"),
        _final("第二轮完成"),
    ])
    session = Session.create("task", tmp_path)
    agent = create_agent(
        llm, resident_skill_tools(registry), session, skills=registry,
    )
    assert agent.run("发布") == "第一轮完成"
    assert agent.run("另一件事") == "第二轮完成"
    transcript = [
        str(record.message.get("content", ""))
        for record in session.message_records
    ]
    assert sum("<skill-catalog>" in text for text in transcript) == 0
    assert len(_catalog_texts(llm.seen_messages[0])) == 1
    assert len(_catalog_texts(llm.seen_messages[1])) == 1


def test_restored_session_uses_live_catalog_not_stale_transcript(tmp_path: Path):
    registry = _write_skill(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = FileSessionRepository(tmp_path / "checkpoints")
    session = Session.create("task", workspace)
    session.begin_user_turn("准备发布")
    session.append_message({"role": "user", "content": "准备发布"})
    session.append_message({
        "role": "user",
        "content": (
            "<system-reminder>\n<skill-catalog>\n- stale-skill: 过期目录\n"
            "</skill-catalog>\n</system-reminder>"
        ),
    })
    store.save(session)

    restored = store.load(session.session_id)
    llm = ScriptLLM([_final("继续")])
    answer = create_agent(
        llm, resident_skill_tools(registry), restored, skills=registry,
    ).continue_run()
    assert answer == "继续"
    catalogs = _catalog_texts(llm.seen_messages[0])
    assert len(catalogs) == 1
    assert "release-check" in catalogs[0]
    assert "stale-skill" not in catalogs[0]
    transcript = [
        str(record.message.get("content", ""))
        for record in restored.message_records
    ]
    assert sum("<skill-catalog>" in text for text in transcript) == 1
