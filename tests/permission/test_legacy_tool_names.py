"""Rules saved or configured with legacy tool names keep exactly their scope.

A rule naming a tool that no longer exists would never match. For deny and
ask that would silently open access, so legacy names resolve to current ones.
"""

from wright.domain.model.tool import ToolAccess
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.settings import (
    MatchContext,
    PermissionRule,
    PermissionSettings,
)


def _shell_context():
    return MatchContext(cwd="/project", execution_mode="sandbox", operations=("shell",))


def test_legacy_shell_deny_rule_still_denies_in_bypass():
    settings = PermissionSettings.from_dict({
        "version": 2,
        "mode": "bypass",
        "permissions": {"deny": ["execute_command(rm *)"]},
    })
    assert settings.deny[0].tool_name == "shell"
    assert settings.deny[0].kind == "shell"

    decision = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"shell"}), subject="rm demo"),
        tool_name="shell", subject="rm demo", in_scope=True, context=_shell_context(),
    )[0]

    assert decision == "deny"


def test_legacy_ask_rule_still_asks():
    settings = PermissionSettings.from_dict({
        "version": 2,
        "mode": "bypass",
        "permissions": {"ask": ["execute_command(git push*)"]},
    })
    decision = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"shell"}), subject="git push origin"),
        tool_name="shell", subject="git push origin", in_scope=True, context=_shell_context(),
    )[0]

    assert decision == "ask"


def test_legacy_file_rule_names_the_current_tool_only():
    rule = PermissionRule.parse("read_file(secrets/*)", effect="deny")

    assert rule.tool_name == "read"
    assert rule.kind == "file"
    assert PermissionRule.parse("write_file").matches("write", "anything")
    assert not PermissionRule.parse("write_file").matches("read", "anything")


def test_persisted_legacy_grants_load_under_current_names():
    network = PermissionRule.sandbox_network_grant().to_persistent()
    assert network["tool_name"] == "shell"
    network["tool_name"] = "execute_command"
    assert PermissionRule.from_mapping(network) == PermissionRule.sandbox_network_grant()

    command = PermissionRule(
        "execute_command", kind="command", argv=("/usr/bin/git",), command_match="executable",
        cwd="/project", sandbox_key="key", executable_target="/usr/bin/git", operations=("shell",),
    )
    command.validate_grant()
    assert command.tool_name == "shell"
    assert command.to_persistent()["tool_name"] == "shell"
