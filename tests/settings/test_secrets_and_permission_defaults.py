"""Settings-page edits of API keys and permission defaults."""

from __future__ import annotations

import json
import os
import stat

import pytest

from wright.application.composition.settings import permission_defaults
from wright.application.permission.defaults import PermissionDefaultsError
from wright.application.settings.secrets import SecretsService
from wright.core.paths import user_permission_settings_path
from wright.domain.model.settings import SettingsError
from wright.infrastructure.config.secret_store import DotenvSecretStore


@pytest.fixture
def secrets(tmp_path):
    environ: dict[str, str] = {}
    store = DotenvSecretStore(tmp_path / ".env", environ=environ)
    return SecretsService(store), store, environ


def test_a_key_is_written_privately_and_never_read_back(secrets):
    service, store, environ = secrets
    store.path().write_text("# my keys\nOTHER=1\nexport TAVILY_API_KEY=old\n", encoding="utf-8")
    view = service.set("TAVILY_API_KEY", "  tvly-new  ")
    assert "tvly-new" not in json.dumps(view)
    assert next(item for item in view["secrets"] if item["name"] == "TAVILY_API_KEY")["source"] == "file"
    text = store.path().read_text(encoding="utf-8")
    assert text == "# my keys\nOTHER=1\nTAVILY_API_KEY='tvly-new'\n"
    assert stat.S_IMODE(os.stat(store.path()).st_mode) == 0o600
    # The next session sees it without a restart.
    assert environ["TAVILY_API_KEY"] == "tvly-new"

    service.set("TAVILY_API_KEY", None)
    assert "TAVILY_API_KEY" not in store.path().read_text(encoding="utf-8")
    assert "TAVILY_API_KEY" not in environ


def test_only_known_keys_and_plain_values_are_written(secrets):
    service, _, _ = secrets
    with pytest.raises(SettingsError):
        service.set("WRIGHT_HOME", "/tmp/x")
    for bad in ("a'b", "a\nb", 'a"b', "a\\b"):
        with pytest.raises(SettingsError):
            service.set("DEEPSEEK_API_KEY", bad)


def test_a_key_set_outside_the_file_is_left_alone(secrets):
    service, _, environ = secrets
    environ["ANTHROPIC_API_KEY"] = "from-shell"
    row = next(item for item in service.describe()["secrets"] if item["name"] == "ANTHROPIC_API_KEY")
    assert row["source"] == "env"
    with pytest.raises(SettingsError):
        service.set("ANTHROPIC_API_KEY", "other")
    assert environ["ANTHROPIC_API_KEY"] == "from-shell"


def test_default_mode_is_written_to_the_user_file_and_bad_modes_are_refused(tmp_path):
    defaults = permission_defaults(tmp_path)
    assert defaults.describe()["mode"] == "default"
    assert defaults.set_mode("acceptEdits")["mode"] == "acceptEdits"
    stored = json.loads(user_permission_settings_path().read_text(encoding="utf-8"))
    assert stored["mode"] == "acceptEdits"
    with pytest.raises(PermissionDefaultsError):
        defaults.set_mode("yolo")
    assert defaults.describe()["mode"] == "acceptEdits"


def test_rules_can_be_tightened_and_builtins_stay_locked(tmp_path):
    defaults = permission_defaults(tmp_path)
    view = defaults.add_rule("shell(terraform destroy*)", "deny", "project")
    added = next(row for row in view["rules"] if row["text"] == "shell(terraform destroy*)")
    assert added["scope"] == "project" and added["effect"] == "deny" and not added["locked"]
    with pytest.raises(PermissionDefaultsError):
        defaults.add_rule("read(/etc/hosts)", "allow", "user")
    # A rule the parser cannot bound is refused and nothing is written.
    with pytest.raises(PermissionDefaultsError):
        defaults.add_rule("web_fetch(https://example.com*)", "deny", "user")
    locked = next(row for row in view["rules"] if row["locked"])
    with pytest.raises(PermissionDefaultsError):
        defaults.remove_rule(locked["id"], locked["scope"])
    after = defaults.remove_rule(added["id"], "project")
    assert all(row["text"] != "shell(terraform destroy*)" for row in after["rules"])


def test_without_a_project_only_the_user_layer_is_edited():
    defaults = permission_defaults(None)
    assert defaults.describe()["project"] is False
    with pytest.raises(PermissionDefaultsError):
        defaults.add_rule("shell(curl *)", "ask", "project")
    assert any(row["text"] == "shell(curl *)" for row in defaults.add_rule("shell(curl *)", "ask", "user")["rules"])
