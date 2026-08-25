import json
from pathlib import Path

from ...agent import Agent
from ...checkpoint import SessionCheckpointStore
from ...events import ContentDone
from ...renderer import SilentRenderer
from ...session import SessionState
from ...skills.registry import SkillRegistry
from ...skills.store import write_skill
from ...tools.skill_tools import build_skill_tools


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages = []

    def __call__(self, messages):
        self.seen_messages.append(list(messages))
        yield ContentDone(content=self.script.pop(0))


def _tool(name, **arguments):
    return json.dumps({
        "tool_calls": [{"name": name, "arguments": arguments}],
        "final_answer": None,
    }, ensure_ascii=False)


def _final(answer):
    return json.dumps({"tool_calls": [], "final_answer": answer}, ensure_ascii=False)


def _write_skill(directory: Path) -> SkillRegistry:
    write_skill(
        directory,
        "release-check",
        name="发布前检查",
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


def test_parent_and_child_sessions_isolate_catalog_flag(tmp_path: Path):
    parent = SessionState.create("parent", tmp_path)
    child = SessionState.create("child", tmp_path)
    parent.mark_skill_catalog_sent()
    assert parent.skill_catalog_sent is True
    assert child.skill_catalog_sent is False


def test_empty_skills_directory_injects_nothing(tmp_path: Path):
    llm = ScriptLLM([_final("done")])
    session = SessionState.create("task", tmp_path)
    Agent(llm, [], session, SilentRenderer(), skills=SkillRegistry(tmp_path)).run("hi")
    assert not any(
        "<skill-catalog>" in str(message.get("content", ""))
        for batch in llm.seen_messages
        for message in batch
    )
    assert not any(
        "<system-reminder>" in str(record.message.get("content", ""))
        for record in session.message_records
    )
    assert session.skill_catalog_sent is True


def test_catalog_is_written_once_into_transcript(tmp_path: Path):
    registry = _write_skill(tmp_path)
    llm = ScriptLLM([
        _tool("skill", skill_id="release-check"),
        _final("已按流程检查"),
    ])
    session = SessionState.create("task", tmp_path)
    tools = build_skill_tools(registry)
    answer = Agent(
        llm, tools, session, SilentRenderer(), skills=registry
    ).run("准备发布")

    assert answer == "已按流程检查"
    assert session.skill_catalog_sent is True

    first_catalogs = _catalog_texts(llm.seen_messages[0])
    assert len(first_catalogs) == 1
    assert "release-check" in first_catalogs[0]
    assert "调用 skill 工具" in first_catalogs[0]

    second_catalogs = _catalog_texts(llm.seen_messages[1])
    assert len(second_catalogs) == 1
    assert "发布前必须先跑测试" in str(llm.seen_messages[1][-1].get("content", ""))

    transcript = [
        str(record.message.get("content", ""))
        for record in session.message_records
    ]
    assert sum("<skill-catalog>" in text for text in transcript) == 1
    assert any("发布前必须先跑测试" in text for text in transcript)


def test_second_user_turn_does_not_resend_catalog(tmp_path: Path):
    registry = _write_skill(tmp_path)
    llm = ScriptLLM([
        _final("第一轮完成"),
        _final("第二轮完成"),
    ])
    session = SessionState.create("task", tmp_path)
    agent = Agent(
        llm,
        build_skill_tools(registry),
        session,
        SilentRenderer(),
        skills=registry,
    )
    assert agent.run("发布") == "第一轮完成"
    assert agent.run("另一件事") == "第二轮完成"
    transcript = [
        str(record.message.get("content", ""))
        for record in session.message_records
    ]
    assert sum("<skill-catalog>" in text for text in transcript) == 1
    assert len(_catalog_texts(llm.seen_messages[1])) == 1


def test_continue_run_keeps_catalog_and_does_not_resend(tmp_path: Path):
    registry = _write_skill(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    session = SessionState.create("task", workspace)
    session.begin_user_turn("准备发布")
    session.append_message({"role": "user", "content": "准备发布"})
    session.append_message({
        "role": "user",
        "content": "<system-reminder>\n<skill-catalog>\n- release-check: 发布\n</skill-catalog>\n</system-reminder>",
    })
    session.mark_skill_catalog_sent()
    store.save(session)

    restored = store.load(session.session_id)
    assert restored.skill_catalog_sent is True
    llm = ScriptLLM([_final("继续")])
    answer = Agent(
        llm, [], restored, SilentRenderer(), skills=registry
    ).continue_run()
    assert answer == "继续"
    transcript = [
        str(record.message.get("content", ""))
        for record in restored.message_records
    ]
    assert sum("<skill-catalog>" in text for text in transcript) == 1
