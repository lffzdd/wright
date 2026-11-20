"""memory.enabled, memory.recall and memory.auto_extract."""

from __future__ import annotations

import pytest

from tests.memory.helpers import write
from tests.memory.test_tools_and_manager import _setup
from tests.settings.test_settings import _write
from wright.application.composition.runtime import (
    RuntimeConfig,
    assemble_runtime,
    shutdown_runtime,
)
from wright.application.memory import manager as manager_module
from wright.core.paths import user_settings_path
from wright.domain.model.settings import SETTING_SPECS


def _fail(*_args, **_kwargs):
    raise AssertionError("no model call expected")


def test_recall_off_skips_the_selector_but_keeps_the_resident_block(tmp_path, monkeypatch):
    manager, _tools = _setup(tmp_path)
    manager.turn_recall = False
    assert write(manager.service, "Deploys go through staging first.", kind="constraint", space=manager.repo_space_id, pin=True).fact
    monkeypatch.setattr(manager_module, "prepare_turn_context", _fail)

    context = manager._prepare_context("how do deploys work", project_id=manager.current_project_id)
    assert context.fact_text == "" and context.episode_text == ""
    assert context.generation == manager.service.generation
    assert "Deploys go through staging first." in manager.resident_block()
    [call] = manager.meter.selections
    assert (call.status, call.reason_codes) == ("skipped", ("disabled",))


def test_auto_extract_off_makes_no_extraction_call(tmp_path, monkeypatch):
    manager, _tools = _setup(tmp_path)
    manager.auto_extract = False
    monkeypatch.setattr(manager_module, "extract_from_snapshot", _fail)
    snapshot = {"root_turn_id": "t", "user_texts": ["以后都用 pnpm"], "episode": {"root_run_id": "r"}}
    assert manager._extract_snapshot(object(), snapshot) == 0
    [call] = manager.meter.extractions
    assert (call.status, call.reason_codes) == ("skipped", ("disabled",))


@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_leaves_memory_out_when_disabled_but_keeps_the_egress_check(tmp_path, monkeypatch, enabled):
    for spec in SETTING_SPECS:
        for name in spec.env:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WRIGHT_AUTO_TITLE", "0")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _write(user_settings_path(), {
        "llm": {"model": "m", "base_url": "http://127.0.0.1:9/v1"},
        "memory": {"enabled": enabled},
    })
    runtime = assemble_runtime(RuntimeConfig(workspace=workspace), start_automation=False)
    try:
        names = {tool.name for tool in runtime.agent._schema_tools}
        assert ("memory_write" in names) is enabled
        assert (runtime.agent.memory is not None) is enabled
        assert runtime.agent.services.egress_guard is not None
    finally:
        shutdown_runtime(runtime)
