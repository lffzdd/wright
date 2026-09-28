from pathlib import Path

from jsonschema import validators

from wright.application.skills import SkillRegistry
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.domain.model.session import Session
from wright.infrastructure.storage.skills import write_skill
from wright.infrastructure.tools.skill_tools import build_skill_tools
from wright.infrastructure.tools.validation import validate_tool_arguments


def _runtime(tmp_path: Path, registry: SkillRegistry | None = None):
    session = Session.create("goal", tmp_path)
    tools = build_skill_tools(registry or SkillRegistry(tmp_path))
    runtime = tool_runtime_for_session(session, workspace_dir=tmp_path)
    return session, tools, runtime


def test_skill_returns_full_body_and_rejects_unknown(tmp_path: Path):
    write_skill(
        tmp_path,
        "release-check",
        name="release-check",
        description="发布时使用",
        body="先跑测试",
        allowed_tools=["execute_command"],
    )
    _session, tools, runtime = _runtime(tmp_path)
    skill = {tool.name: tool for tool in tools}["load_skill"]

    loaded = skill.call({"skill_id": "release-check"}, runtime)
    assert loaded.ok
    assert loaded.data["skill_id"] == "release-check"
    assert loaded.data["name"] == "release-check"
    assert loaded.data["body"] == "先跑测试"
    assert loaded.data["allowed_tools"] == ["execute_command"]
    assert loaded.retention == "instruction"
    assert loaded.retention_key == "skill:release-check"
    root = Path(loaded.data["skill_root"])
    assert loaded.data["source_path"] == str(root / "SKILL.md")
    assert root == (tmp_path / "release-check").resolve()

    missing = skill.call({"skill_id": "nope"}, runtime)
    assert not missing.ok
    assert "Unknown skill" in missing.err


def test_project_skill_root_wins_over_user_skill(tmp_path: Path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    write_skill(project, "shared", name="shared", description="项目技能", body="看 scripts/check.py")
    write_skill(user, "shared", name="shared", description="用户技能", body="用户步骤")
    (project / "shared" / "scripts").mkdir()
    (project / "shared" / "scripts" / "check.py").write_text("project-script", encoding="utf-8")
    (user / "shared" / "scripts").mkdir()
    (user / "shared" / "scripts" / "check.py").write_text("user-script", encoding="utf-8")
    registry = SkillRegistry([project, user])
    _session, tools, runtime = _runtime(tmp_path, registry)
    loaded = tools[0].call({"skill_id": "shared"}, runtime)
    root = Path(loaded.data["skill_root"])
    assert root == (project / "shared").resolve()
    assert Path(loaded.data["source_path"]) == root / "SKILL.md"
    assert (root / "scripts" / "check.py").read_text(encoding="utf-8") == "project-script"


def test_skill_tool_schema_is_valid():
    tools = build_skill_tools(SkillRegistry(Path(".")))
    assert [tool.name for tool in tools] == ["load_skill"]
    tool = tools[0]
    validator_cls = validators.validator_for(tool.parameters)
    validator_cls.check_schema(tool.parameters)
    assert validate_tool_arguments(tool, {"skill_id": "ok"}) is None
    invalid = validate_tool_arguments(tool, {"skill_id": "../x"})
    assert invalid is not None
    assert invalid.data["error"]["type"] == "tool_input_validation"
