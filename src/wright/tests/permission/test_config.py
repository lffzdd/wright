import json
from pathlib import Path

from ...executor import ToolExecutor
from ...permission import (
    PermissionCheckResult,
    PermissionResolver,
    PermissionRule,
    PermissionSettings,
    RuleBasedApprovalHandler,
    append_allow_rule,
    load_permission_settings,
)
from ...tools.base import Tool, ToolCall, ToolResult


def _ask_tool(name: str, risk_flags: tuple[str, ...]) -> Tool:
    return Tool(
        name=name,
        description="",
        parameters={},
        call=lambda args, runtime: ToolResult.success({"called": True, "args": args}),
        check_permission=lambda args, runtime: PermissionCheckResult(
            "ask", f"{name}: needs approval", risk_flags, source="tool"
        ),
    )


def _executor(tool: Tool, settings: PermissionSettings, tmp_path: Path) -> ToolExecutor:
    resolver = PermissionResolver(
        approval_handler=RuleBasedApprovalHandler(settings)
    )
    return ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
        permission_resolver=resolver,
    )


def _run(executor: ToolExecutor, call: ToolCall):
    return executor.execute([call])[0].result


# ── 规则解析 ──────────────────────────────────────────────────────────────────

def test_bare_rule_matches_any_call():
    rule = PermissionRule.parse("write_file")
    assert rule.matches("write_file", "anything")
    assert not rule.matches("edit_file", "anything")


def test_glob_rule_matches_subject():
    rule = PermissionRule.parse("execute_command(git status*)")
    assert rule.matches("execute_command", "git status -s")
    assert not rule.matches("execute_command", "git push origin")


# ── default 模式:allow 命中放行,未命中 fail-closed ─────────────────────────

def test_default_allow_rule_permits(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "default", "permissions": {"allow": ["write_file"]}}
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), settings, tmp_path)
    result = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    assert result.ok and result.data["called"]


def test_default_no_rule_denies(tmp_path):
    settings = PermissionSettings.from_dict({"mode": "default", "permissions": {}})
    executor = _executor(_ask_tool("write_file", ("writes_files",)), settings, tmp_path)
    result = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    assert not result.ok
    assert result.data["permission"]["decision"] == "deny"
    assert result.data["permission"]["source"] == "rule_config"


def test_deny_rule_overrides_allow(tmp_path):
    settings = PermissionSettings.from_dict(
        {
            "mode": "default",
            "permissions": {
                "allow": ["execute_command"],
                "deny": ["execute_command(rm *)"],
            },
        }
    )
    executor = _executor(
        _ask_tool("execute_command", ("executes_shell",)), settings, tmp_path
    )
    assert _run(executor, ToolCall("execute_command", {"command": "ls"}, "c1")).ok
    blocked = _run(executor, ToolCall("execute_command", {"command": "rm -rf x"}, "c2"))
    assert not blocked.ok
    assert "deny 规则" in blocked.data["permission"]["reason"]


# ── 模式语义 ──────────────────────────────────────────────────────────────────

def test_bypass_allows_without_rules(tmp_path):
    settings = PermissionSettings.from_dict({"mode": "bypass", "permissions": {}})
    executor = _executor(
        _ask_tool("execute_command", ("executes_shell",)), settings, tmp_path
    )
    assert _run(executor, ToolCall("execute_command", {"command": "ls"}, "c1")).ok


def test_bypass_still_respects_deny(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "bypass", "permissions": {"deny": ["execute_command(rm *)"]}}
    )
    executor = _executor(
        _ask_tool("execute_command", ("executes_shell",)), settings, tmp_path
    )
    assert not _run(
        executor, ToolCall("execute_command", {"command": "rm x"}, "c1")
    ).ok


def test_plan_denies_all_side_effects(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "plan", "permissions": {"allow": ["write_file"]}}
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), settings, tmp_path)
    result = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    assert not result.ok
    assert "plan" in result.data["permission"]["reason"]


def test_accept_edits_allows_file_writes_only(tmp_path):
    settings = PermissionSettings.from_dict({"mode": "acceptEdits", "permissions": {}})
    file_exec = _executor(
        _ask_tool("write_file", ("writes_files",)), settings, tmp_path
    )
    cmd_exec = _executor(
        _ask_tool("execute_command", ("writes_files", "executes_shell")),
        settings,
        tmp_path,
    )
    assert _run(file_exec, ToolCall("write_file", {"file": "a.txt"}, "c1")).ok
    # 掺了 shell 副作用 → 不在纯编辑放行范围
    assert not _run(
        cmd_exec, ToolCall("execute_command", {"command": "ls"}, "c2")
    ).ok


# ── 系统边界:越出 workspace 任何模式都拒 ─────────────────────────────────────

def test_cwd_outside_workspace_denied_even_in_bypass(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = PermissionSettings.from_dict({"mode": "bypass", "permissions": {}})
    resolver = PermissionResolver(
        approval_handler=RuleBasedApprovalHandler(settings)
    )
    executor = ToolExecutor(
        {"write_file": _ask_tool("write_file", ("writes_files",))},
        workspace_dir=workspace,
        cwd_provider=lambda: tmp_path,  # cwd 在 workspace 之外
        permission_resolver=resolver,
    )
    result = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    assert not result.ok
    assert "workspace" in result.data["permission"]["reason"]


# ── 配置加载 ──────────────────────────────────────────────────────────────────

def test_load_settings_from_file(tmp_path):
    cfg = tmp_path / "perm.json"
    cfg.write_text(
        json.dumps(
            {"mode": "acceptEdits", "permissions": {"allow": ["write_file"]}}
        ),
        encoding="utf-8",
    )
    settings = load_permission_settings(cfg)
    assert settings.mode == "acceptEdits"
    assert settings.allow[0].tool_name == "write_file"


def test_missing_file_falls_back_to_locked_default(tmp_path):
    settings = load_permission_settings(tmp_path / "nope.json")
    assert settings.mode == "default"
    assert settings.allow == [] and settings.deny == []


def test_invalid_mode_raises(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text(json.dumps({"mode": "yolo"}), encoding="utf-8")
    try:
        load_permission_settings(cfg)
    except ValueError as e:
        assert "yolo" in str(e)
    else:
        raise AssertionError("invalid mode should raise")


# ── #1 ask 规则:强制询问,压过 allow / 模式自动放行,deny 仍盖过它 ─────────────

def test_ask_rule_overrides_allow(tmp_path):
    # allow 了整个 write_file,但 ask 在敏感目录上挖洞 → 该路径强制询问(此处无人 → 拒)
    settings = PermissionSettings.from_dict(
        {
            "mode": "default",
            "permissions": {"allow": ["write_file"], "ask": ["write_file(secrets/*)"]},
        }
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), settings, tmp_path)

    normal = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    sensitive = _run(executor, ToolCall("write_file", {"file": "secrets/k.txt"}, "c2"))

    assert normal.ok  # 普通路径走 allow
    assert not sensitive.ok  # 命中 ask → resolver fail-closed
    assert sensitive.data["permission"]["decision"] == "ask"


def test_ask_rule_overrides_bypass_mode(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "bypass", "permissions": {"ask": ["write_file"]}}
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), settings, tmp_path)
    result = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    assert result.data["permission"]["decision"] == "ask"  # ask 压过 bypass


def test_deny_still_beats_ask(tmp_path):
    settings = PermissionSettings.from_dict(
        {"mode": "default", "permissions": {"ask": ["write_file"], "deny": ["write_file"]}}
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), settings, tmp_path)
    result = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    assert result.data["permission"]["decision"] == "deny"  # deny 最强


# ── #2 别再问:把放行固化进配置文件 ───────────────────────────────────────────

def test_append_allow_rule_persists_and_dedups(tmp_path):
    cfg = tmp_path / "perm.json"
    cfg.write_text(
        json.dumps({"mode": "default", "permissions": {"allow": ["edit_file"]}}),
        encoding="utf-8",
    )

    append_allow_rule("write_file", cfg)
    append_allow_rule("write_file", cfg)  # 第二次应去重,不重复写入

    reloaded = load_permission_settings(cfg)
    names = [r.tool_name for r in reloaded.allow]
    assert names.count("write_file") == 1
    assert "edit_file" in names


def test_append_allow_rule_creates_missing_file(tmp_path):
    cfg = tmp_path / "new.json"
    append_allow_rule("write_file", cfg)
    assert cfg.is_file()
    assert load_permission_settings(cfg).allow[0].tool_name == "write_file"
