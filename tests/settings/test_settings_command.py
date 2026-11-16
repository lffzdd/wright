"""The /settings slash command shared by the CLI REPL and the TUI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from wright.application.session.dispatch import dispatch_slash, slash_command_matches
from wright.core.paths import project_settings_path, user_settings_path
from wright.domain.model.settings import SETTING_SPECS
from wright.interfaces.tui.slash import tui_help_text


class _Notices:
    def __init__(self) -> None:
        self.notices: list[str] = []

    def on_system_notice(self, text: str, *, code: str = "", params: dict | None = None) -> None:
        self.notices.append(text)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for spec in SETTING_SPECS:
        for name in spec.env:
            monkeypatch.delenv(name, raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    renderer = _Notices()
    rt = SimpleNamespace(
        event_renderer=renderer,
        project_context=SimpleNamespace(project_root=project),
    )
    return rt, renderer, project


def test_lists_every_key_with_its_source(runtime, monkeypatch):
    rt, renderer, project = runtime
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    assert dispatch_slash("/settings", rt) is True
    text = renderer.notices[-1]
    for spec in SETTING_SPECS:
        assert spec.key in text
    assert "env-model" in text and "env OPENAI_MODEL" in text
    assert str(project_settings_path(project)) in text
    assert "user-only" in text


def test_set_and_unset_write_the_right_file(runtime):
    rt, renderer, project = runtime
    dispatch_slash("/settings set llm.models a, b", rt)
    assert "Saved llm.models = a,b" in renderer.notices[-1]
    dispatch_slash("/settings set --project llm.model project-model", rt)
    assert json.loads(project_settings_path(project).read_text()) == {"llm": {"model": "project-model"}}
    assert "Effective: project-model (project)" in renderer.notices[-1]
    dispatch_slash("/settings set session.auto_title off", rt)
    assert json.loads(user_settings_path().read_text())["session"] == {"auto_title": False}

    dispatch_slash("/settings unset session.auto_title", rt)
    assert "Removed session.auto_title" in renderer.notices[-1]
    assert "session" not in json.loads(user_settings_path().read_text())


def test_mentions_an_env_override(runtime, monkeypatch):
    rt, renderer, _project = runtime
    monkeypatch.setenv("WRIGHT_LLM_TRANSPORT", "chat")
    dispatch_slash("/settings set llm.transport responses", rt)
    assert "WRIGHT_LLM_TRANSPORT is set and overrides" in renderer.notices[-1]


@pytest.mark.parametrize(("line", "match"), [
    ("/settings set --project llm.base_url https://attacker.invalid", "user settings file"),
    ("/settings set llm.context_limit lots", "integer"),
    ("/settings set llm.nope x", "Unknown setting"),
    ("/settings set llm.model", "Usage"),
    ("/settings frobnicate", "Usage"),
])
def test_rejects_bad_edits_without_writing(runtime, line, match):
    rt, renderer, project = runtime
    assert dispatch_slash(line, rt) is True
    assert "rejected" in renderer.notices[-1] and match in renderer.notices[-1]
    assert not user_settings_path().exists()
    assert not project_settings_path(project).exists()


def test_command_is_offered_in_completion_and_tui_help():
    assert [command.name for command in slash_command_matches("/sett")] == ["/settings"]
    assert "/settings" in tui_help_text()
