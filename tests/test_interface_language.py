"""Interface language is separate from model instructions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wright.application.agent.context import _is_persisted_memory_recall
from wright.application.memory.prompt import (
    build_memory_instructions,
    project_core_memory,
)
from wright.application.skills.prompt import catalog_reminder
from wright.domain.model.skills import SkillMeta
from wright.domain.model.tool import ToolAccess
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.settings import PermissionSettings
from wright.domain.policy.verifier import VerificationIssue, VerificationResult
from wright.domain.prompt import build_system_prompt
from wright.infrastructure.tools.human_input.ask_user import ask_user_tool
from wright.interfaces.i18n import (
    activate_saved_locale,
    get_locale,
    present,
    set_locale,
    t,
)


@pytest.fixture(autouse=True)
def _reset_locale():
    set_locale("en")
    yield
    set_locale("en")


def test_default_and_invalid_locale_are_english():
    assert get_locale() == "en"
    assert set_locale("nope") == "en"
    assert t("cli.choice_invalid", choice="x") == "Invalid choice: x"
    assert t("not.a.real.key") == "not.a.real.key"
    set_locale("zh-CN")
    assert t("cli.choice_invalid", choice="2") == "无效的选择：2"
    assert present("missing.code", {}, fallback="keep this") == "keep this"


def test_preference_persists_outside_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path))
    assert activate_saved_locale() == "en"
    assert not (tmp_path / "preferences.json").exists()
    set_locale("zh-CN", persist=True)
    set_locale("en")
    assert activate_saved_locale() == "zh-CN"
    saved = json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))
    assert saved["interface_language"] == "zh-CN"


def test_model_instructions_ignore_interface_language():
    english = build_system_prompt(())
    assert english.count("Follow the user's language.") == 1
    schema = json.dumps(ask_user_tool.to_schema() if hasattr(ask_user_tool, "to_schema") else {
        "description": ask_user_tool.description,
        "parameters": ask_user_tool.parameters,
    })
    settings = PermissionSettings.from_dict({"version": 2, "mode": "plan"})
    denied = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"file_write"})),
        tool_name="write_file",
        subject="a.txt",
        in_scope=True,
    )
    rejected = VerificationResult.reject((
        VerificationIssue("incomplete", "The verifier did not explain the rejection"),
    ))
    feedback = rejected.feedback_message()["content"]
    set_locale("zh-CN")
    assert build_system_prompt(()) == english
    again = json.dumps(ask_user_tool.to_schema() if hasattr(ask_user_tool, "to_schema") else {
        "description": ask_user_tool.description,
        "parameters": ask_user_tool.parameters,
    })
    assert again == schema
    assert "Ask the user" in schema
    denied_again = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"file_write"})),
        tool_name="write_file",
        subject="a.txt",
        in_scope=True,
    )
    assert denied_again == denied
    assert denied[0] == "deny"
    assert "plan" in denied[1].lower()
    assert denied[3]
    assert rejected.feedback_message()["content"] == feedback
    assert "Do not repeat the same final answer" in feedback
    assert "计划" not in feedback


def test_user_and_skill_text_stay_in_their_own_language(tmp_path: Path):
    instructions = build_memory_instructions(tmp_path)
    projected = project_core_memory(instructions, "用户喜欢用中文写提交说明")
    assert "Long-term memory" in projected
    assert "用户喜欢用中文写提交说明" in projected
    catalog = catalog_reminder([
        SkillMeta(
            id="hanyu",
            name="hanyu",
            description="用中文解释汉字",
            allowed_tools=(),
            path=tmp_path,
        )
    ])
    assert "用中文解释汉字" in catalog
    assert "skill catalog" in catalog
    old = {
        "role": "user",
        "content": "<system-reminder>\n历史记忆数据\n历史执行经历\n</system-reminder>",
    }
    current = {
        "role": "user",
        "content": '<system-reminder source="wright-semantic-recall">\n用户记得这件事\n</system-reminder>',
    }
    assert _is_persisted_memory_recall(old) is False
    assert _is_persisted_memory_recall(current) is True
