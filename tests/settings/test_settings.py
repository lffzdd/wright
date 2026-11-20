"""Typed, layered settings: precedence, project restrictions, legacy env names."""

from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from wright.application.composition.settings import load_settings, settings_service
from wright.core.logger import get_logger, set_configured_terminal_level
from wright.core.paths import project_settings_path, user_settings_path
from wright.domain.model.settings import SETTING_SPECS, SettingsError, WrightSettings
from wright.domain.policy.settings import resolve_settings
from wright.infrastructure.config.settings_store import JsonSettingsStore

_LEGACY = {
    "OPENAI_MODEL": ("llm.model", "m", "m"),
    "WRIGHT_MODELS": ("llm.models", "a, b,a", ("a", "b")),
    "OPENAI_MEMORY_MODEL": ("llm.memory_model", "small", "small"),
    "OPENAI_CONTEXT_LIMIT": ("llm.context_limit", "64000", 64000),
    "WRIGHT_CONTEXT_LIMITS": ("llm.context_limits", "my-model=64000", ("my-model=64000",)),
    "WRIGHT_MODEL_PRICES": ("llm.prices", "kimi-k3=3/15/0.3, my-model=1/2", ("kimi-k3=3/15/0.3", "my-model=1/2")),
    "WRIGHT_LLM_TRANSPORT": ("llm.transport", "Chat", "chat"),
    "OPENAI_BASE_URL": ("llm.base_url", "http://127.0.0.1:9/v1", "http://127.0.0.1:9/v1"),
    "WRIGHT_AUTO_TITLE": ("session.auto_title", "off", False),
    "WRIGHT_MEMORY_ENABLED": ("memory.enabled", "off", False),
    "WRIGHT_MEMORY_RECALL": ("memory.recall", "no", False),
    "WRIGHT_MEMORY_AUTO_EXTRACT": ("memory.auto_extract", "0", False),
    "WRIGHT_LOG_LEVEL": ("logging.level", "debug", "DEBUG"),
    "WRIGHT_KNOWLEDGE_ENABLED": ("knowledge.enabled", "1", True),
    "WRIGHT_KNOWLEDGE_RETRIEVER": ("knowledge.retriever", "hybrid", "hybrid"),
    "WRIGHT_KNOWLEDGE_RERANKER": ("knowledge.reranker", "yes", True),
    "WRIGHT_RAG_DIR": ("knowledge.rag_dir", "/opt/rag", "/opt/rag"),
    "WRIGHT_KNOWLEDGE_INDEX": ("knowledge.index", "/opt/rag/i.json", "/opt/rag/i.json"),
}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def clean_env(monkeypatch):
    for spec in SETTING_SPECS:
        for name in spec.env:
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_every_key_has_a_legacy_env_name_and_each_one_maps():
    assert {spec.env[0] for spec in SETTING_SPECS} == set(_LEGACY)
    for name, (key, raw, expected) in _LEGACY.items():
        resolved = resolve_settings(environ={name: raw})
        assert resolved.settings.value(key) == expected, name
        assert resolved.settings.sources[key] == "env"
        assert resolved.warnings == ()


def test_defaults_without_any_layer():
    settings = resolve_settings().settings
    assert settings == WrightSettings()
    # 0: each model's context window comes from the catalog, then 128000.
    assert settings.llm.context_limit == 0
    assert settings.session.auto_title is True
    assert set(settings.sources.values()) == {"default"}


def test_layers_apply_in_order_user_project_env():
    user = {"llm": {"model": "user-model", "context_limit": 1000, "memory_model": "u"}}
    project = {"llm": {"model": "project-model", "context_limit": 2000}}
    environ = {"OPENAI_CONTEXT_LIMIT": "3000", "OPENAI_MODEL": ""}
    settings = resolve_settings(user=user, project=project, environ=environ).settings
    assert settings.llm.model == "project-model"
    assert settings.llm.context_limit == 3000
    assert settings.llm.memory_model == "u"
    assert settings.sources["llm.model"] == "project"
    assert settings.sources["llm.context_limit"] == "env"
    assert settings.sources["llm.memory_model"] == "user"


@pytest.mark.parametrize("key", [
    spec.key for spec in SETTING_SPECS if not spec.project_safe
])
def test_project_file_cannot_set_user_only_keys(key):
    section, _, name = key.partition(".")
    spec = next(item for item in SETTING_SPECS if item.key == key)
    sample = {"list": ["x"], "bool": True, "int": 5}.get(spec.kind, spec.choices[-1] if spec.choices else "/evil")
    resolved = resolve_settings(project={section: {name: sample}})
    assert resolved.settings.value(key) == spec.default
    assert resolved.settings.sources[key] == "default"
    assert any(key in warning and "user settings file" in warning for warning in resolved.warnings)


def test_project_safe_keys_are_only_model_choices():
    assert {spec.key for spec in SETTING_SPECS if spec.project_safe} == {
        "llm.model", "llm.context_limit", "llm.context_limits", "llm.memory_model",
    }


@pytest.mark.parametrize("layer", ["user", "project"])
def test_secret_looking_keys_are_rejected(layer):
    resolved = resolve_settings(**{layer: {"llm": {"api_key": "sk-123"}, "openai_api_key": "x"}})
    assert len(resolved.warnings) == 2
    assert all("secrets belong" in warning for warning in resolved.warnings)


def test_invalid_value_falls_back_with_a_warning():
    resolved = resolve_settings(
        user={"llm": {"context_limit": 5000}},
        project={"llm": {"context_limit": "many"}},
        environ={"WRIGHT_LOG_LEVEL": "verbose"},
    )
    assert resolved.settings.llm.context_limit == 5000
    assert resolved.settings.logging.level == "WARNING"
    assert len(resolved.warnings) == 2


def test_unknown_keys_warn():
    resolved = resolve_settings(user={"llm": {"modle": "x"}, "extra": 1})
    assert resolved.settings == WrightSettings()
    assert len(resolved.warnings) == 2
    assert all("unknown setting" in warning for warning in resolved.warnings)


@pytest.mark.parametrize("user", [
    {"llm": {"base_url": 123}},
    {"knowledge": {"rag_dir": ["/a"]}},
    {"knowledge": {"index": ""}},
])
def test_invalid_endpoint_or_path_in_user_file_fails_closed(user):
    with pytest.raises(SettingsError, match="Invalid"):
        resolve_settings(user=user)


def test_invalid_endpoint_in_project_file_is_only_ignored():
    resolved = resolve_settings(
        user={"llm": {"base_url": "https://trusted.invalid/v1"}},
        project={"llm": {"base_url": "https://attacker.invalid"}},
    )
    assert resolved.settings.llm.base_url == "https://trusted.invalid/v1"


def test_store_reads_both_files_for_a_project(tmp_path, clean_env):
    project = tmp_path / "repo"
    _write(user_settings_path(), {"llm": {"model": "u", "transport": "chat"}})
    _write(project_settings_path(project), {"llm": {"model": "p", "transport": "responses"}})
    settings = load_settings(project)
    assert settings.llm.model == "p"
    assert settings.llm.transport == "chat"
    assert load_settings(None).llm.model == "u"


def test_env_beats_files(tmp_path, clean_env):
    _write(user_settings_path(), {"llm": {"model": "u"}})
    clean_env.setenv("OPENAI_MODEL", "from-env")
    assert load_settings(tmp_path).llm.model == "from-env"


def test_unreadable_user_file_is_an_error_but_project_file_is_not(tmp_path, clean_env):
    project = tmp_path / "repo"
    project_settings_path(project).parent.mkdir(parents=True)
    project_settings_path(project).write_text("{not json", encoding="utf-8")
    resolved = settings_service(project).resolve()
    assert resolved.settings == WrightSettings()
    assert any("Ignored project settings" in warning for warning in resolved.warnings)

    user_settings_path().parent.mkdir(parents=True, exist_ok=True)
    user_settings_path().write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SettingsError, match="JSON object"):
        load_settings(project)


def test_write_sets_and_removes_keys_atomically(tmp_path, clean_env):
    project = tmp_path / "repo"
    service = settings_service(project)
    service.update("user", "llm.models", "a,b")
    service.update("user", "session.auto_title", False)
    service.update("project", "llm.model", "p")
    assert json.loads(user_settings_path().read_text()) == {
        "llm": {"models": ["a", "b"]}, "session": {"auto_title": False},
    }
    assert stat.S_IMODE(os.stat(user_settings_path()).st_mode) == 0o600
    assert json.loads(project_settings_path(project).read_text()) == {"llm": {"model": "p"}}

    service.update("user", "session.auto_title", None)
    assert json.loads(user_settings_path().read_text()) == {"llm": {"models": ["a", "b"]}}
    assert not list(user_settings_path().parent.glob(".settings.json.*.tmp"))


@pytest.mark.parametrize(("scope", "key", "value", "match"), [
    ("project", "llm.base_url", "https://attacker.invalid", "user settings file"),
    ("project", "knowledge.rag_dir", "/tmp/x", "user settings file"),
    ("user", "llm.api_key", "sk", "secrets belong"),
    ("user", "llm.nope", "x", "unknown setting"),
    ("user", "llm.context_limit", -1, "at least 0"),
    ("user", "llm.transport", "grpc", "one of"),
])
def test_write_refuses_invalid_edits(tmp_path, clean_env, scope, key, value, match):
    service = settings_service(tmp_path / "repo")
    with pytest.raises(SettingsError, match=match):
        service.update(scope, key, value)
    assert not user_settings_path().exists()
    assert not project_settings_path(tmp_path / "repo").exists()


def test_project_write_without_a_project_is_refused(clean_env):
    with pytest.raises(SettingsError, match="No project"):
        settings_service(None).update("project", "llm.model", "x")


def test_describe_reports_layer_and_env_override(tmp_path, clean_env):
    project = tmp_path / "repo"
    _write(user_settings_path(), {"llm": {"model": "u"}})
    clean_env.setenv("WRIGHT_LLM_TRANSPORT", "chat")
    view = settings_service(project).describe()
    items = {item["key"]: item for item in view["settings"]}
    assert items["llm.model"]["value"] == "u"
    assert items["llm.model"]["source"] == "user"
    assert items["llm.model"]["user_value"] == "u"
    assert items["llm.model"]["scopes"] == ["user", "project"]
    assert items["llm.transport"]["env_override"] is True
    assert items["llm.base_url"]["scopes"] == ["user"]
    assert view["paths"]["project"] == str(project_settings_path(project))


def test_store_uses_the_given_environment(tmp_path):
    store = JsonSettingsStore(tmp_path, environ={"OPENAI_MODEL": "x"})
    assert store.environment() == {"OPENAI_MODEL": "x"}


def test_log_level_from_user_file_reaches_the_terminal_filter(tmp_path, clean_env, monkeypatch):
    _write(user_settings_path(), {"logging": {"level": "INFO"}})
    logger = get_logger("wright.test.settings_logging")
    terminal = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler))
    shown = []
    monkeypatch.setattr(terminal, "emit", lambda record: shown.append(record.getMessage()))
    try:
        load_settings(tmp_path)
        logger.info("visible")
        monkeypatch.setenv("WRIGHT_LOG_LEVEL", "ERROR")
        logger.warning("hidden by env")
    finally:
        set_configured_terminal_level(None)
    assert shown == ["visible"]


def test_runtime_is_built_from_the_settings_files(tmp_path, clean_env):
    from wright.application.composition.runtime import (
        RuntimeConfig,
        assemble_runtime,
        shutdown_runtime,
    )

    clean_env.setenv("OPENAI_API_KEY", "test-key")
    clean_env.setenv("WRIGHT_AUTO_TITLE", "0")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    # load_env() reads the .env in the current directory; run from the fixture
    # workspace so a developer's own .env cannot override the project settings.
    clean_env.chdir(workspace)
    _write(user_settings_path(), {
        "llm": {
            "model": "user-model", "base_url": "http://127.0.0.1:9/v1",
            "models": ["user-model", "other"], "memory_model": "small",
        },
        "knowledge": {"enabled": True},
    })
    _write(project_settings_path(workspace), {
        "llm": {"model": "project-model", "context_limit": 4096,
                "base_url": "https://attacker.invalid"},
    })
    runtime = assemble_runtime(RuntimeConfig(workspace=workspace), start_automation=False)
    try:
        assert runtime.llm.model == "project-model"
        assert runtime.llm.context_limit == 4096
        assert runtime.llm.base_url == "http://127.0.0.1:9/v1"
        assert runtime.settings.llm.models == ("user-model", "other")
        assert runtime.title_llm is None
        assert "knowledge_search" in {tool.name for tool in runtime.assembled_base_tools}
    finally:
        shutdown_runtime(runtime)


def test_runtime_refuses_an_unreadable_user_file(tmp_path, clean_env):
    from wright.application.composition.runtime import RuntimeConfig, assemble_runtime

    user_settings_path().parent.mkdir(parents=True, exist_ok=True)
    user_settings_path().write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit, match="Could not load settings"):
        assemble_runtime(RuntimeConfig(workspace=tmp_path, model="m"), start_automation=False)
