from pathlib import Path

from jsonschema import validators

from ...session import SessionState
from ...skills.registry import SkillRegistry
from ...skills.store import write_skill
from ...tools.base import ToolRuntime
from ...tools.skill_tools import build_skill_tools
from ...tools.validation import validate_tool_arguments


def _runtime(tmp_path: Path, registry: SkillRegistry | None = None):
    session = SessionState.create("goal", tmp_path)
    tools = build_skill_tools(registry or SkillRegistry(tmp_path))
    runtime = ToolRuntime(workspace_dir=tmp_path, session_state=session)
    return session, tools, runtime


def test_skill_returns_full_body_and_rejects_unknown(tmp_path: Path):
    write_skill(
        tmp_path,
        "release-check",
        name="发布前检查",
        description="发布时使用",
        body="先跑测试",
        allowed_tools=["execute_command"],
    )
    _session, tools, runtime = _runtime(tmp_path)
    skill = {tool.name: tool for tool in tools}["skill"]

    loaded = skill.call({"skill_id": "release-check"}, runtime)
    assert loaded.ok
    assert loaded.data["skill_id"] == "release-check"
    assert loaded.data["name"] == "发布前检查"
    assert loaded.data["body"] == "先跑测试"
    assert loaded.data["allowed_tools"] == ["execute_command"]

    missing = skill.call({"skill_id": "nope"}, runtime)
    assert not missing.ok
    assert "未知 skill" in missing.err


def test_skill_tool_schema_is_valid():
    tools = build_skill_tools(SkillRegistry(Path(".")))
    assert [tool.name for tool in tools] == ["skill"]
    tool = tools[0]
    validator_cls = validators.validator_for(tool.parameters)
    validator_cls.check_schema(tool.parameters)
    assert validate_tool_arguments(tool, {"skill_id": "ok"}) is None
    invalid = validate_tool_arguments(tool, {"skill_id": "../x"})
    assert invalid is not None
    assert invalid.data["error"]["type"] == "tool_input_validation"
