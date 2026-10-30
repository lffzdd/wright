"""Bounded remembered command identities and HTTP target choices."""

from dataclasses import replace
from pathlib import Path

import pytest
from scripts.generate_permission_fixtures import FixturePaths

from wright.application.permission.workflow import AuthorizationWorkflow
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.settings import (
    MatchContext,
    PermissionRule,
    PermissionSettings,
    command_segments,
    direct_command_argv,
)
from wright.domain.policy.permission.types import InvocationIdentity, PermissionResponse
from wright.infrastructure.runtime.local import LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.web_tools import http_request_tool, web_search_tool


@pytest.mark.parametrize(
    "command,expected",
    [
        ("python -c 'print(1); print(2)'", ("python", "-c", "print(1); print(2)")),
        ('python "a b"', ("python", "a b")),
        (r"python a\ b", ("python", "a b")),
        (
            "python -c 'print(\"$HOME | & >\")'",
            ("python", "-c", 'print("$HOME | & >")'),
        ),
        ('python ""', ("python", "")),
    ],
)
def test_literal_argv_is_quote_aware(command, expected):
    assert direct_command_argv(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "python a; curl host",
        "python a | cat",
        "python a > out",
        "python $(whoami)",
        'python "$HOME"',
        "X=1 python a",
        "env python a",
        "sudo python a",
        "python *.py",
        "python `whoami`",
        "python a\npython b",
        r'python "a\$b"',
        r'python "a\`b"',
        'python "a\\\nb"',
        "py\\\nthon -V",
    ],
)
def test_shell_syntax_falls_back_to_exact(command):
    assert direct_command_argv(command) is None


def command_rule(**changes):
    return replace(
        PermissionRule(
            "execute_command",
            kind="command",
            argv=("/usr/bin/python3",),
            cwd="/project",
            sandbox_key="sandbox",
            operations=("shell",),
            executable_target="/usr/bin/python3",
        ),
        **changes,
    )


def test_command_rule_identity_cwd_capability_and_dialect():
    context = MatchContext(
        cwd="/project",
        sandbox_key="sandbox",
        executable="/usr/bin/python3",
        executable_target="/usr/bin/python3",
        execution_mode="sandbox",
    )
    rule = command_rule()
    assert rule.matches("execute_command", "python3 -c 'print(1)'", context=context)
    # Approving a program survives cd and added directories; the sandbox still bounds it.
    for unchanged in (replace(context, cwd="/elsewhere"), replace(context, sandbox_key="changed")):
        assert rule.matches("execute_command", "python3 -V", context=unchanged)
    for changed in (
        replace(context, executable="/other/python3"),
        replace(context, execution_mode="host"),
        replace(context, cwd="C:\\project"),
    ):
        assert not rule.matches("execute_command", "python3 -V", context=changed)
    assert not rule.matches("execute_command", "python3 -V && evil", context=context)
    prefix = command_rule(argv=("/usr/bin/git", "status"), command_match="argv_prefix")
    git_context = replace(context, executable="/usr/bin/git")
    assert prefix.matches("execute_command", "git status --short", context=git_context)
    assert not prefix.matches("execute_command", "git statusx", context=git_context)
    assert not prefix.matches("execute_command", "git push", context=git_context)


def test_command_codec_rejects_unknown_and_unbounded_records():
    for lifetime in ("session", "project", "user"):
        rule = command_rule(lifetime=lifetime)
        assert PermissionRule.from_mapping(rule.to_persistent()) == rule
        assert PermissionSettings.from_dict(
            {"version": 2, "permissions": {"allow": [rule.to_persistent()]}}
        ).allow == [rule]
    for change in (
        {"kind": "unknown"},
        {"argv": ["python"]},
        {"argv": []},
        {"dialect": "powershell"},
        {"sandbox_key": ""},
    ):
        with pytest.raises((ValueError, TypeError)):
            PermissionRule.from_mapping({**command_rule().to_persistent(), **change})


def test_execute_command_allow_list_accepts_command_and_bounded_shell_only():
    command = command_rule(lifetime="user")
    loaded = PermissionSettings.from_dict(
        {"version": 2, "permissions": {"allow": [command.to_persistent()]}}
    )
    assert loaded.allow == [command]
    shell = PermissionRule(
        "execute_command",
        kind="shell",
        pattern="echo ok",
        exact=True,
        lifetime="session",
        sandbox_key="sandbox",
        cwd="/project",
    )
    PermissionSettings.from_dict(
        {"version": 2, "permissions": {"allow": [shell.to_persistent()]}}
    )
    loose_shell = replace(shell, exact=False)
    with pytest.raises(ValueError, match="Shell grants require"):
        PermissionSettings.from_dict(
            {"version": 2, "permissions": {"allow": [loose_shell.to_persistent()]}}
        )
    with pytest.raises(ValueError, match="command grants or exact session shell"):
        PermissionSettings.from_dict(
            {"version": 2, "permissions": {"allow": ["execute_command"]}}
        )


class Paths(FixturePaths):
    def resolve_executable(self, program, *, cwd):
        return "/usr/bin/" + program if "/" not in program else program


def resolve(tool, arguments, choice=None, *, backend=None, rules=(), settings=None):
    handler = (lambda request: PermissionResponse(choice)) if choice else None
    return AuthorizationWorkflow(PermissionPolicy(settings), handler).resolve(
        ToolCall(tool.name, arguments, "call"),
        tool,
        backend=backend or Paths(),
        scope=AccessScope(Path("/project")),
        identity=InvocationIdentity("session"),
        session_rules=rules,
    )


def test_command_choices_and_exact_legacy_fallback():
    result = resolve(execute_command_tool, {"command": "python3 -c 'print(1)'"})
    choices = {item.id: item for item in result.prompt.choices}
    assert "allow_session_rule" in choices
    for lifetime in ("session", "project", "user"):
        chosen = choices[f"allow_{lifetime}_command_executable"]
        assert chosen.resource["argv"] == ["/usr/bin/python3"]
        allowed = resolve(
            execute_command_tool, {"command": "python3 -c 'print(1)'"}, chosen.id
        )
        assert allowed.grant.command == "python3 -c 'print(1)'"
        assert allowed.grant.command_argv == ("/usr/bin/python3", "-c", "print(1)")
    assert not any("prefix" in item for item in choices)
    for backend, command in (
        (FixturePaths(), "unresolved_program -V"),
        (Paths(), "python -V && $(evil)"),
    ):
        prompt = resolve(
            execute_command_tool, {"command": command}, backend=backend
        ).prompt
        assert not any(item.resource_kind == "command" for item in prompt.choices)
    git = resolve(execute_command_tool, {"command": "git status --short"})
    assert any(item.id == "allow_project_command_prefix" for item in git.prompt.choices)


def test_remembered_command_issues_canonical_launch_and_deny_still_wins():
    rule = command_rule(argv=("/usr/bin/python3",), sandbox_key="placeholder")
    approved = resolve(
        execute_command_tool,
        {"command": "python3 -V"},
        "allow_session_command_executable",
    )
    rule = approved.changes.session_rules[0]
    reused = resolve(
        execute_command_tool, {"command": "python3 -c 'print(2)'"}, rules=(rule,)
    )
    assert reused.decision == "allow"
    assert reused.grant.command_argv == ("/usr/bin/python3", "-c", "print(2)")
    denied = resolve(
        execute_command_tool,
        {"command": "python3 -V"},
        rules=(rule,),
        settings=PermissionSettings(deny=[PermissionRule("execute_command")]),
    )
    assert denied.decision == "deny"
    network = resolve(
        execute_command_tool, {"command": "python3 -V", "network": True}, rules=(rule,)
    )
    assert network.decision == "deny"  # approval required; no handler supplied


def test_http_path_choices_exclude_query_and_preserve_origin_semantics():
    result = resolve(
        http_request_tool,
        {"url": "https://example.com/api/item?secret=1", "method": "GET"},
    )
    choices = {item.id: item for item in result.prompt.choices}
    path_rule = PermissionRule.from_mapping(
        dict(choices["allow_project_network_path"].resource)
    )
    origin_rule = PermissionRule.from_mapping(
        dict(choices["allow_project_rule"].resource)
    )
    assert path_rule.path == "/api/item" and path_rule.path_match == "exact"
    for query in ("a=1", "other=2"):
        assert path_rule.matches(
            "http_request",
            "",
            context=MatchContext(
                url=f"https://example.com/api/item?{query}", method="GET"
            ),
        )
    assert not path_rule.matches(
        "http_request",
        "",
        context=MatchContext(url="https://example.com/api/other", method="GET"),
    )
    assert not path_rule.matches(
        "http_request",
        "",
        context=MatchContext(url="https://example.com/api/item", method="POST"),
    )
    assert origin_rule.matches(
        "http_request",
        "",
        context=MatchContext(url="https://example.com/other", method="GET"),
    )
    old = origin_rule.to_persistent()
    old.pop("path")
    old.pop("path_match")
    assert PermissionRule.from_mapping(old).path_match == "origin"
    write = resolve(
        http_request_tool, {"url": "https://example.com/api", "method": "POST"}
    )
    assert [item.id for item in write.prompt.choices] == ["allow_once", "deny"]


def test_named_search_grants_remain_operation_bounded():
    result = resolve(web_search_tool, {"query": "test"}, "allow_project_rule")
    rule = result.changes.project_rules[0]
    assert rule.kind == "tool" and rule.operations == ("network_read",)
    assert (
        resolve(web_search_tool, {"query": "new query"}, rules=(rule,)).decision
        == "allow"
    )
    assert not rule.matches(
        "http_request", "", context=MatchContext(operations=("network_read",))
    )
    assert not rule.matches(
        "web_search", "", context=MatchContext(operations=("network_write",))
    )


def test_local_program_resolution_observes_clean_path_and_cwd(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        program = directory / "myprogram"
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    monkeypatch.setenv("PATH", str(first))
    assert backend.resolve_executable("myprogram", cwd=backend.cwd()) == str(
        first / "myprogram"
    )
    monkeypatch.setenv("PATH", str(second))
    assert backend.resolve_executable("myprogram", cwd=backend.cwd()) == str(
        second / "myprogram"
    )


@pytest.mark.parametrize(("command", "expected"), [
    ("python3 a.py && echo done", (("python3", "a.py"),)),
    ("cd sub; python3 a.py 2>/dev/null | head -3", (("python3", "a.py"), ("head", "-3"))),
    ("python3 -c 'a; b | c' || uv --version", (("python3", "-c", "a; b | c"), ("uv", "--version"))),
    ("ls *.py", (("ls", "*.py"),)),
    ("echo a; pwd", ()),
])
def test_compound_commands_name_their_programs(command, expected):
    assert command_segments(command) == expected


@pytest.mark.parametrize("command", [
    "python3 a.py && $(evil)", "python3 `evil`", "echo \"$(evil)\"", "python3 a.py > out", "(python3 a.py)",
    "python3 a.py &", "sudo python3 a.py", "python3 a.py | xargs rm", "X=1 python3 a.py", "$PY a.py",
    "cd sub && ./run", "python3 a.py # note", "python3 <<EOF\nx\nEOF",
])
def test_compound_commands_that_stay_whole_command_approvals(command):
    assert command_segments(command) is None


def test_session_program_rules_cover_compound_commands_but_not_new_programs():
    rule = PermissionRule("execute_command", kind="command", argv=("/usr/bin/python3",), command_match="executable",
                          cwd="/elsewhere", sandbox_key="other", operations=("shell",), executable_target="/usr/bin/python3")
    allowed = resolve(execute_command_tool, {"command": "python3 -V && echo ok"}, rules=(rule,))
    assert allowed.decision == "allow" and allowed.grant.command_argv == ()
    missing = resolve(execute_command_tool, {"command": "python3 -V && uv --version"}, rules=(rule,))
    assert missing.decision != "allow"
    assert [item.resource["argv"] for item in missing.prompt.choices if item.id == "allow_session_command_executable"] == [["/usr/bin/uv"]]
    both = resolve(execute_command_tool, {"command": "python3 -V && uv --version"})
    chosen = next(item for item in both.prompt.choices if item.id == "allow_session_command_set")
    assert chosen.resource["programs"] == ["/usr/bin/python3", "/usr/bin/uv"]
    remembered = resolve(execute_command_tool, {"command": "python3 -V && uv --version"}, chosen.id)
    assert [item.argv[0] for item in remembered.changes.session_rules] == ["/usr/bin/python3", "/usr/bin/uv"]
