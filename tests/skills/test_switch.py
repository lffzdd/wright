"""skills.disabled: switched-off skills leave the catalog and load_skill refuses them."""

from pathlib import Path

import pytest

from tests.runtime_helpers import tool_runtime_for_session
from wright.application.agent.prompt import AgentPromptManager
from wright.application.composition.settings import (
    disabled_skills,
    write_disabled_skills,
)
from wright.application.skills import EnabledSkills
from wright.domain.model.session import Session
from wright.domain.model.settings import SettingsError
from wright.domain.model.skills import SkillNotFoundError
from wright.infrastructure.storage.skill_registry import SkillRegistry
from wright.infrastructure.storage.skills import write_skill
from wright.infrastructure.tools.skill_tools import build_skill_tools


def _skills(tmp_path: Path, *ids: str) -> Path:
    directory = tmp_path / "skills"
    for skill_id in ids:
        write_skill(directory, skill_id, name=skill_id, description=f"about {skill_id}", body=f"steps of {skill_id}")
    return directory


def test_disabled_skill_leaves_catalog_and_cannot_be_loaded(tmp_path: Path):
    off: list[str] = ["beta"]
    catalog = EnabledSkills(SkillRegistry(_skills(tmp_path, "alpha", "beta")), lambda: off)
    session = Session.create("goal", tmp_path)
    tools = build_skill_tools(catalog)
    manager = AgentPromptManager(session, skills=catalog, schema_tools=tools)

    text = manager.skill_catalog_message()["content"]
    assert "- alpha:" in text and "beta" not in text
    refused = tools[0].call({"skill_id": "beta"}, tool_runtime_for_session(session, workspace_dir=tmp_path))
    assert not refused.ok and "turned off" in refused.err
    assert [meta.id for meta in catalog.all_metas()] == ["alpha", "beta"]

    # With every skill off the loader keeps its schema, so the tool list and
    # the cached prefix stay as they were; only the catalog goes away.
    off.append("alpha")
    assert [tool.name for tool in manager.visible_schema_tools()] == ["load_skill"]
    assert manager.skill_catalog_message() is None


def test_switch_writes_project_settings_and_applies_on_next_read(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    catalog = EnabledSkills(
        SkillRegistry(_skills(tmp_path, "alpha", "beta")),
        disabled_skills(project),
        write_disabled_skills(project),
    )

    catalog.set_enabled("beta", False)
    assert catalog.disabled() == {"beta"}
    assert '"beta"' in (project / ".wright" / "settings.json").read_text(encoding="utf-8")
    assert [meta.id for meta in catalog.list_metas()] == ["alpha"]

    catalog.set_enabled("beta", True)
    assert catalog.disabled() == frozenset()
    assert [meta.id for meta in catalog.list_metas()] == ["alpha", "beta"]


def test_switch_refuses_unknown_skill_and_env_override(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    catalog = EnabledSkills(SkillRegistry(_skills(tmp_path, "alpha")), disabled_skills(project), write_disabled_skills(project))
    with pytest.raises(SkillNotFoundError, match="Unknown skill"):
        catalog.set_enabled("nope", False)

    monkeypatch.setenv("WRIGHT_DISABLED_SKILLS", "alpha")
    with pytest.raises(SettingsError, match="WRIGHT_DISABLED_SKILLS"):
        catalog.set_enabled("alpha", True)
    assert catalog.disabled() == {"alpha"}
