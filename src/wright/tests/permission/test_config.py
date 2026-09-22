import json
from pathlib import Path

from ...engine.executor import ToolExecutor
from ...permission import (
    PermissionPolicy,
    PermissionResolver,
    PermissionRule,
    PermissionSettings,
    ToolAccess,
    append_additional_directory,
    append_allow_rule,
    load_permission_settings,
)
from ...tools.base import ToolCall
from ...tools.command_tools import execute_command_tool
from ...tools.file_tools import read_file_tool, write_file_tool


def _executor(tool, settings: PermissionSettings, tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
        permission_resolver=PermissionResolver(settings=settings),
    )


def _run(executor: ToolExecutor, call: ToolCall):
    return executor.execute([call])[0].result


def test_rule_parsing_and_shell_composition_guard():
    rule = PermissionRule.parse("execute_command(git status*)")
    assert rule.matches("execute_command", "git status -s")
    assert not rule.matches("execute_command", "git push origin")
    scoped = PermissionRule.parse("execute_command(ls*)")
    assert scoped.matches("execute_command", "ls -la")
    assert not scoped.matches("execute_command", "ls && curl https://example.com")
    assert not scoped.matches("execute_command", "ls > victim")


def test_bare_and_subject_rules_match_only_the_declared_tool():
    assert PermissionRule.parse("write_file").matches("write_file", "anything")
    assert not PermissionRule.parse("write_file").matches("read_file", "anything")
    rule = PermissionRule.parse("execute_command(git status*)")
    assert rule.matches("execute_command", "git status --short")
    assert not rule.matches("execute_command", "git push")


def test_default_allow_rule_permits_write_file(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "default", "permissions": {"allow": ["write_file"]}}
    )
    result = _run(
        _executor(write_file_tool, settings, tmp_path),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert result.ok
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"


def test_default_no_rule_fails_closed(tmp_path):
    settings = PermissionSettings.from_dict({"mode": "default", "permissions": {}})
    result = _run(
        _executor(write_file_tool, settings, tmp_path),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert not result.ok
    assert result.data["permission"]["decision"] == "deny"


def test_global_deny_overrides_tool_allow(tmp_path):
    settings = PermissionSettings.from_dict(
        {
            "mode": "default",
            "permissions": {"allow": ["read_file"], "deny": ["read_file(secrets/*)"]},
        }
    )
    (tmp_path / "secrets").mkdir()
    result = _run(
        _executor(read_file_tool, settings, tmp_path),
        ToolCall("read_file", {"file": "secrets/key"}, "c1"),
    )
    assert not result.ok
    assert "deny 规则" in result.data["permission"]["reason"]


def test_plan_mode_denies_side_effects_even_with_allow(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "plan", "permissions": {"allow": ["write_file"]}}
    )
    result = _run(
        _executor(write_file_tool, settings, tmp_path),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert not result.ok
    assert "plan" in result.data["permission"]["reason"]


def test_bypass_allows_shell_but_ask_rule_still_wins(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "bypass", "permissions": {"ask": ["execute_command"]}}
    )
    result = _run(
        _executor(execute_command_tool, settings, tmp_path),
        ToolCall("execute_command", {"command": "printf ok"}, "c1"),
    )
    assert not result.ok
    assert result.data["permission"]["decision"] == "deny"


def test_accept_edits_only_auto_allows_in_scope_file_edit(tmp_path):
    settings = PermissionSettings.from_dict({"mode": "acceptEdits", "permissions": {}})
    result = _run(
        _executor(write_file_tool, settings, tmp_path),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert result.ok


def test_bypass_allows_without_rules_and_deny_still_wins(tmp_path):
    assert PermissionPolicy(PermissionSettings(mode="bypass")).evaluate(
        ToolAccess(frozenset({"shell"}), subject="printf ok"),
        tool_name="execute_command",
        subject="printf ok",
        in_scope=True,
    )[0] == "allow"

    blocked = _run(
        _executor(
            execute_command_tool,
            PermissionSettings.from_dict({
                "mode": "bypass",
                "permissions": {"deny": ["execute_command(rm *)"]},
            }),
            tmp_path,
        ),
        ToolCall("execute_command", {"command": "rm file"}, "c2"),
    )
    assert not blocked.ok
    assert "deny 规则" in blocked.data["permission"]["reason"]


def test_accept_edits_does_not_auto_allow_shell(tmp_path):
    result = _run(
        _executor(execute_command_tool, PermissionSettings(mode="acceptEdits"), tmp_path),
        ToolCall("execute_command", {"command": "printf no"}, "c1"),
    )
    assert not result.ok


def test_cwd_outside_workspace_does_not_deny_an_origin_target(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = _run(
        ToolExecutor(
            {write_file_tool.name: write_file_tool},
            workspace_dir=workspace,
            cwd_provider=lambda: tmp_path,
            permission_resolver=PermissionResolver(
                settings=PermissionSettings(mode="bypass")
            ),
        ),
        ToolCall(
            "write_file",
            {"file": "workspace/a.txt", "content": "hello"},
            "c1",
        ),
    )
    assert result.ok
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "hello"


def test_policy_keeps_external_read_approval_in_plan_mode():
    settings = PermissionSettings.from_dict({"mode": "plan", "permissions": {}})
    decision = PermissionPolicy(settings).evaluate(
        ToolAccess(
            frozenset({"network_read"}), subject="https://example.com"
        ),
        tool_name="http_request",
        subject="https://example.com",
        in_scope=True,
    )
    assert decision[0] == "ask"


def test_plan_mode_keeps_user_interaction_available():
    settings = PermissionSettings.from_dict({"mode": "plan", "permissions": {}})
    decision = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"user_interaction"}), subject="question"),
        tool_name="ask_user",
        subject="question",
        in_scope=True,
    )
    assert decision[0] == "ask"


def test_settings_round_trip_and_atomic_append(tmp_path):
    config = tmp_path / "permissions.json"
    config.write_text(
        json.dumps({"mode": "acceptEdits", "permissions": {"deny": ["rm"]}}),
        encoding="utf-8",
    )
    append_allow_rule("write_file", config)
    append_additional_directory(str(tmp_path / "extra"), config)
    settings = load_permission_settings(config)
    assert settings.mode == "acceptEdits"
    assert [rule.tool_name for rule in settings.allow] == ["write_file"]
    assert settings.deny[0].tool_name == "rm"
    assert settings.additional_directories == [str((tmp_path / "extra").resolve())]


def test_append_helpers_create_and_deduplicate_settings(tmp_path):
    config = tmp_path / "new-permissions.json"
    append_allow_rule("write_file", config)
    append_allow_rule("write_file", config)
    append_additional_directory(str(tmp_path / "extra"), config)
    append_additional_directory(str(tmp_path / "extra"), config)
    settings = load_permission_settings(config)
    assert [rule.tool_name for rule in settings.allow] == ["write_file"]
    assert settings.additional_directories == [str((tmp_path / "extra").resolve())]


def test_load_settings_from_explicit_file_and_user_path(tmp_path, monkeypatch):
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"mode": "acceptEdits", "permissions": {"allow": ["read_file"]}}),
        encoding="utf-8",
    )
    loaded = load_permission_settings(config)
    assert loaded.mode == "acceptEdits"
    assert loaded.allow[0].tool_name == "read_file"

    monkeypatch.delenv("WRIGHT_PERMISSION_CONFIG", raising=False)
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    user = tmp_path / "home" / "permission_settings.json"
    user.parent.mkdir()
    user.write_text(
        json.dumps({"mode": "default", "permissions": {"allow": ["http_request"]}}),
        encoding="utf-8",
    )
    assert load_permission_settings().allow[0].tool_name == "http_request"


def test_ask_and_deny_rules_have_the_required_precedence():
    access = ToolAccess(frozenset({"file_write"}), subject="secrets/key")
    settings = PermissionSettings.from_dict({
        "mode": "bypass",
        "permissions": {
            "allow": ["write_file"],
            "ask": ["write_file(secrets/*)"],
            "deny": ["write_file(secrets/key)"],
        },
    })
    policy = PermissionPolicy(settings)
    assert policy.evaluate(
        access, tool_name="write_file", subject="secrets/key", in_scope=True
    )[0] == "deny"
    ask_settings = PermissionSettings.from_dict({
        "mode": "bypass",
        "permissions": {
            "allow": ["write_file"],
            "ask": ["write_file(secrets/*)"],
        },
    })
    assert PermissionPolicy(ask_settings).evaluate(
        access, tool_name="write_file", subject="secrets/key", in_scope=True
    )[0] == "ask"


def test_missing_and_invalid_settings(tmp_path):
    assert load_permission_settings(tmp_path / "missing.json").mode == "default"
    bad = tmp_path / "bad.json"
    bad.write_text('{"mode": "yolo"}', encoding="utf-8")
    try:
        load_permission_settings(bad)
    except ValueError as exc:
        assert "yolo" in str(exc)
    else:
        raise AssertionError("invalid mode should raise")
