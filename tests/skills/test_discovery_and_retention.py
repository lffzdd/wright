"""Production skill discovery, retention, and restore.

Scripted model calls capture the request that would be sent. They do not
stand in for a live model.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.responses import event, response
from wright.application.agent import create_agent
from wright.application.composition.roles import tools_for_role
from wright.application.skills import SkillRegistry
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolResult
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.storage.skills import write_skill
from wright.infrastructure.tools.base import Tool
from wright.infrastructure.tools.skill_tools import resident_skill_tools


class ScriptLLM:
    def __init__(self, script, *, context_limit: int = 128_000):
        self.script = list(script)
        self.context_limit = context_limit
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list[list[dict]] = []
        self.on_request = None

    def __call__(self, messages, **kwargs):
        if self.on_request is not None:
            self.on_request(len(self.seen_messages))
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(kwargs.get("tools") or []))
        yield event(content=self.script.pop(0))


def _final(answer: str):
    return response(content=answer, calls=[])


def _call(name: str, **arguments):
    return {"name": name, "arguments": arguments}


def _names(tools: list[dict]) -> list[str]:
    return [item["name"] for item in tools]


def _catalogs(messages: list[dict]) -> list[str]:
    return [
        str(message.get("content", ""))
        for message in messages
        if "<skill-catalog>" in str(message.get("content", ""))
    ]


def _install(directory: Path, *, description: str, body: str) -> SkillRegistry:
    write_skill(
        directory,
        "release-check",
        name="release-check",
        description=description,
        body=body,
        allowed_tools=["execute_command"],
    )
    return SkillRegistry(directory)


def test_production_loader_is_resident_and_first_request_can_load(tmp_path: Path):
    runtime = (
        Path(__file__).resolve().parents[2]
        / "src/wright/application/composition/runtime.py"
    ).read_text(encoding="utf-8")
    assert "resident_skill_tools(" in runtime
    assert "for tool in optional_skill_tools" not in runtime

    registry = _install(
        tmp_path,
        description="release deployment checks before shipping",
        body="STEP-KEEP-RELEASE-GATE",
    )
    tools = resident_skill_tools(registry)
    assert tools and tools[0].name == "load_skill"
    assert tools[0].defer_to_model is False

    llm = ScriptLLM([
        response(calls=[_call("load_skill", skill_id="release-check")]),
        _final("done"),
    ])
    answer = create_agent(
        llm, tools, Session.create("release", tmp_path), skills=registry,
    ).run("ship it")

    assert answer == "done"
    assert "release-check" in _catalogs(llm.seen_messages[0])[0]
    assert "release deployment checks" in _catalogs(llm.seen_messages[0])[0]
    assert "load_skill" in _names(llm.seen_tools[0])
    assert "execute_command" not in _names(llm.seen_tools[1])
    assert "STEP-KEEP-RELEASE-GATE" in json.dumps(llm.seen_messages[1], ensure_ascii=False)


def test_child_and_durable_roles_do_not_gain_skills(tmp_path: Path):
    registry = _install(tmp_path, description="release checks", body="steps")
    tools = resident_skill_tools(registry)
    assert "load_skill" not in {tool.name for tool in tools_for_role(tools, "child")}
    assert "load_skill" not in {tool.name for tool in tools_for_role(tools, "durable")}

    llm = ScriptLLM([_final("child done")])
    create_agent(
        llm, tools_for_role(tools, "child"), Session.create("child", tmp_path),
        skills=registry,
    ).run("do the release")
    assert _catalogs(llm.seen_messages[0]) == []
    assert "load_skill" not in _names(llm.seen_tools[0])


def test_catalog_tracks_file_changes_and_starts_empty(tmp_path: Path):
    registry = SkillRegistry(tmp_path)
    llm = ScriptLLM([
        _final("empty"),
        _final("added"),
        _final("updated"),
        _final("removed"),
    ])
    agent = create_agent(
        llm,
        resident_skill_tools(registry),
        Session.create("files", tmp_path),
        skills=registry,
    )

    assert agent.run("look") == "empty"
    assert _catalogs(llm.seen_messages[0]) == []
    assert "load_skill" not in _names(llm.seen_tools[0])

    write_skill(
        tmp_path, "release-check",
        name="release-check", description="release deployment checks", body="first body",
    )
    assert agent.run("again") == "added"
    assert "release-check" in _catalogs(llm.seen_messages[1])[0]
    assert "release deployment checks" in _catalogs(llm.seen_messages[1])[0]
    assert "load_skill" in _names(llm.seen_tools[1])

    write_skill(
        tmp_path, "release-check",
        name="release-check", description="updated deployment checks", body="second body",
    )
    write_skill(
        tmp_path, "notes",
        name="notes", description="meeting notes", body="take notes",
    )
    assert agent.run("third") == "updated"
    catalog = _catalogs(llm.seen_messages[2])[0]
    assert "updated deployment checks" in catalog
    assert "notes" in catalog
    assert "release deployment checks" not in catalog

    (tmp_path / "release-check" / "SKILL.md").unlink()
    assert agent.run("fourth") == "removed"
    latest = _catalogs(llm.seen_messages[3])[0]
    assert "notes" in latest
    assert "release-check" not in latest
    transcript = "\n".join(
        str(record.message.get("content", "")) for record in agent.session_state.message_records
    )
    assert "<skill-catalog>" not in transcript


def test_skill_steps_remain_in_the_model_request_after_compaction(tmp_path: Path):
    registry = _install(
        tmp_path,
        description="release deployment checks",
        body="STEP-KEEP-RELEASE-GATE",
    )

    def filler(args, runtime):
        del args, runtime
        return ToolResult.success("Z" * 20_000)

    pad = Tool(
        name="pad_result",
        description="Return a large disposable result",
        parameters={"type": "object", "properties": {}},
        call=filler,
    )
    llm = ScriptLLM([
        response(calls=[_call("load_skill", skill_id="release-check")]),
        response(calls=[_call("pad_result"), _call("pad_result"), _call("pad_result")]),
        _final("done"),
    ], context_limit=30_000)
    session = Session.create("compact-skill", tmp_path)
    answer = create_agent(
        llm,
        [*resident_skill_tools(registry), pad],
        session,
        skills=registry,
        context_watermark=0.1,
        keep_recent_tool_results=1,
    ).run("check the release")

    assert answer == "done"
    final_request = json.dumps(llm.seen_messages[-1], ensure_ascii=False)
    assert "STEP-KEEP-RELEASE-GATE" in final_request
    assert "[older tool result folded for this request]" in final_request
    stored = json.dumps(
        [record.message for record in session.message_records], ensure_ascii=False,
    )
    assert "STEP-KEEP-RELEASE-GATE" in stored


def test_repeat_load_keeps_the_latest_body_in_the_request(tmp_path: Path):
    registry = _install(
        tmp_path,
        description="first description",
        body="OLD-STEP " + ("o" * 8_000),
    )
    llm = ScriptLLM([
        response(calls=[_call("load_skill", skill_id="release-check")]),
        response(calls=[_call("load_skill", skill_id="release-check")]),
        _final("done"),
    ], context_limit=40_000)

    def refresh(index: int) -> None:
        if index == 1:
            write_skill(
                tmp_path,
                "release-check",
                name="release-check",
                description="second description",
                body="NEW-STEP follow this exact procedure",
            )

    llm.on_request = refresh
    create_agent(
        llm,
        resident_skill_tools(registry),
        Session.create("reload", tmp_path),
        skills=registry,
        context_watermark=0.05,
        keep_recent_tool_results=0,
    ).run("reload the skill")

    last = json.dumps(llm.seen_messages[-1], ensure_ascii=False)
    assert "NEW-STEP follow this exact procedure" in last
    assert "OLD-STEP" not in last
    assert "second description" in _catalogs(llm.seen_messages[-1])[0]
    assert "superseded" in last


def test_budget_failure_reports_that_instructions_were_not_folded(tmp_path: Path):
    registry = _install(
        tmp_path,
        description="release deployment checks",
        body="BUDGET-STEP " + ("b" * 12_000),
    )
    llm = ScriptLLM([
        response(calls=[_call("load_skill", skill_id="release-check")]),
        _final("should not be called"),
    ])
    llm.on_request = lambda index: setattr(llm, "context_limit", 400)
    session = Session.create("budget", tmp_path)
    agent = create_agent(
        llm, resident_skill_tools(registry), session, skills=registry,
    )
    notices: list[str] = []
    agent.ui.publisher.add_listener(
        lambda event: notices.append(str(event.payload.get("content", "")))
        if event.type == "content.final" else None
    )

    assert agent.run("load it") is None
    assert any("required instruction content" in text for text in notices)
    assert any("上下文无法在预算内安全构建" in text for text in notices)
    sent = json.dumps(llm.seen_messages, ensure_ascii=False)
    assert "BUDGET-STEP" not in sent
    assert "[older tool result folded for this request]" not in sent
    stored = json.dumps(
        [record.message for record in session.message_records], ensure_ascii=False,
    )
    assert "BUDGET-STEP" in stored


def test_restore_keeps_catalog_body_and_activated_tools(tmp_path: Path):
    skills = tmp_path / "skills"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = _install(
        skills,
        description="release deployment checks",
        body="RESTORED-STEP",
    )
    deferred = Tool(
        name="create_schedule",
        description="Create a durable recurring scheduled task",
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success({"scheduled": True}),
        defer_to_model=True,
    )
    store = FileSessionRepository(tmp_path / "checkpoints")
    session = Session.create("restore", workspace)
    session.active_deferred_tools = ["create_schedule"]
    llm = ScriptLLM([
        response(calls=[_call("load_skill", skill_id="release-check")]),
        _final("loaded"),
    ])
    assert create_agent(
        llm,
        [*resident_skill_tools(registry), deferred],
        session,
        skills=registry,
        checkpoint_store=store,
    ).run("load") == "loaded"

    restored = store.load(session.session_id)
    assert restored.active_deferred_tools == ["create_schedule"]
    follow = ScriptLLM([_final("resumed")])
    assert create_agent(
        follow,
        [*resident_skill_tools(registry), deferred],
        restored,
        skills=registry,
        checkpoint_store=store,
    ).run("continue") == "resumed"
    request = json.dumps(follow.seen_messages[0], ensure_ascii=False)
    assert "RESTORED-STEP" in request
    assert "release-check" in _catalogs(follow.seen_messages[0])[0]
    assert "create_schedule" in _names(follow.seen_tools[0])
    saved = json.loads(
        (tmp_path / "checkpoints" / f"{session.session_id}.json").read_text(encoding="utf-8")
    )
    assert "skill_catalog_sent" not in saved["session"]
    assert "retention" in json.dumps(saved["session"]["message_records"], ensure_ascii=False)
