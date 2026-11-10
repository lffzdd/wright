"""Sandbox network is a capability grant, not a bypass default."""

from pathlib import Path

import pytest
from scripts.generate_permission_fixtures import FixturePaths

from wright.application.permission.resources import GrantError, resource_change
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.domain.model.tool import ToolAccess, ToolCall
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.settings import (
    MatchContext,
    PermissionRule,
    PermissionSettings,
)
from wright.domain.policy.permission.types import InvocationIdentity, PermissionResponse
from wright.infrastructure.runtime.local import LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.web_tools import http_request_tool


def _network_access(command="uv sync"):
    return ToolAccess(frozenset({"shell", "network_write"}), subject=command)


def _context(mode="sandbox"):
    return MatchContext(cwd="/project", execution_mode=mode, operations=("shell", "network_write"))


def test_sandbox_network_grant_round_trips():
    rule = replace_lifetime(PermissionRule.sandbox_network_grant(), "project")
    assert PermissionRule.from_mapping(rule.to_persistent()) == rule
    loaded = PermissionSettings.from_dict({"version": 2, "permissions": {"allow": [rule.to_persistent()]}})
    assert loaded.allow == [rule]


def replace_lifetime(rule, lifetime):
    from dataclasses import replace
    return replace(rule, lifetime=lifetime)


def test_sandbox_network_grant_rejects_host_or_wrong_ops():
    with pytest.raises(ValueError, match="Sandbox network grants"):
        PermissionRule.from_mapping({**PermissionRule.sandbox_network_grant().to_persistent(), "execution_mode": "host"})
    with pytest.raises(ValueError, match="Sandbox network grants"):
        PermissionRule.from_mapping({**PermissionRule.sandbox_network_grant().to_persistent(), "operations": ["network_read"]})


def test_bypass_still_asks_for_network_without_grant():
    decision, _, _, code, _ = PermissionPolicy(PermissionSettings(mode="bypass")).evaluate(
        _network_access(), tool_name="shell", subject="uv sync", in_scope=True, context=_context(),
    )
    assert decision == "ask"
    assert code == "permission.network_requested"


def test_sandbox_network_grant_lets_bypass_allow_networked_shell():
    grant = PermissionRule.sandbox_network_grant()
    decision = PermissionPolicy(PermissionSettings(mode="bypass")).evaluate(
        _network_access(), tool_name="shell", subject="uv sync", in_scope=True,
        context=_context(), session_rules=(grant,),
    )[0]
    assert decision == "allow"


def test_sandbox_network_grant_does_not_skip_shell_approval_in_default_mode():
    grant = PermissionRule.sandbox_network_grant()
    decision, _, _, code, _ = PermissionPolicy(PermissionSettings(mode="default")).evaluate(
        _network_access(), tool_name="shell", subject="uv sync", in_scope=True,
        context=_context(), session_rules=(grant,),
    )
    assert decision == "ask"
    assert code == "permission.needs_approval"


def test_sandbox_network_grant_does_not_allow_host_shell():
    grant = PermissionRule.sandbox_network_grant()
    decision, _, _, code, _ = PermissionPolicy(PermissionSettings(mode="bypass")).evaluate(
        _network_access(), tool_name="shell", subject="uv sync", in_scope=True,
        context=_context("host"), session_rules=(grant,),
    )
    assert decision == "ask"
    assert code == "permission.needs_approval"


def test_deny_still_wins_over_sandbox_network_grant():
    grant = PermissionRule.sandbox_network_grant()
    settings = PermissionSettings.from_dict({
        "version": 2,
        "mode": "bypass",
        "permissions": {"deny": ["shell(rm *)"], "allow": [grant.to_persistent()]},
    })
    decision = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"shell", "network_write"}), subject="rm file"),
        tool_name="shell", subject="rm file", in_scope=True, context=_context(),
    )[0]
    assert decision == "deny"


def test_sandbox_network_grant_does_not_allow_http():
    grant = PermissionRule.sandbox_network_grant()
    decision = PermissionPolicy(PermissionSettings(mode="bypass")).evaluate(
        ToolAccess(frozenset({"network_read"}), subject="https://example.com"),
        tool_name="http_request", subject="https://example.com", in_scope=True,
        session_rules=(grant,),
    )[0]
    assert decision == "ask"


def resolve(arguments, choice=None, *, rules=(), settings=None, execution_mode="sandbox"):
    handler = (lambda request: PermissionResponse(choice)) if choice else None
    return AuthorizationWorkflow(
        PermissionPolicy(settings), handler, execution_mode=execution_mode,
    ).resolve(
        ToolCall("shell", arguments, "call"),
        execute_command_tool,
        backend=FixturePaths(),
        scope=AccessScope(Path("/project")),
        identity=InvocationIdentity("session"),
        session_rules=rules,
    )


def test_sandbox_shell_prompt_offers_remembered_network():
    prompt = resolve({"command": "echo hello"}).prompt
    ids = {choice.id for choice in prompt.choices}
    assert "allow_once_network" in ids
    assert "allow_session_sandbox_network" in ids
    assert "allow_project_sandbox_network" in ids
    assert "allow_user_sandbox_network" in ids


def test_host_prompt_does_not_offer_sandbox_network():
    ids = {choice.id for choice in resolve({"command": "echo hello"}, execution_mode="host").prompt.choices}
    assert "allow_once_network" not in ids
    assert "allow_session_sandbox_network" not in ids


def test_allow_once_network_does_not_save_and_enables_network():
    result = resolve({"command": "echo hello", "network": True}, "allow_once_network")
    assert result.decision == "allow"
    assert result.grant.network_enabled is True
    assert result.changes.session_rules == ()


def test_allow_once_on_networked_command_stays_offline():
    result = resolve({"command": "echo hello", "network": True}, "allow_once")
    assert result.decision == "allow"
    assert result.grant.network_enabled is False


def test_remembering_sandbox_network_enables_this_call_and_later_bypass():
    first = resolve({"command": "uv sync", "network": True}, "allow_session_sandbox_network")
    assert first.decision == "allow"
    assert first.grant.network_enabled is True
    assert first.changes.session_rules[0].kind == "sandbox_network"
    later = resolve(
        {"command": "uv pip install x", "network": True},
        settings=PermissionSettings(mode="bypass"),
        rules=first.changes.session_rules,
    )
    assert later.decision == "allow"
    assert later.grant.network_enabled is True
    assert later.source == "mode"


def test_sandbox_network_grant_without_bypass_still_asks_for_the_command():
    grant = replace_lifetime(PermissionRule.sandbox_network_grant(), "session")
    result = resolve({"command": "uv sync", "network": True}, rules=(grant,))
    assert result.decision == "deny"
    assert result.prompt.reason_code == "permission.needs_approval"


def test_resource_change_can_add_sandbox_network(tmp_path):
    from wright.domain.model.session import Session

    session = Session.create("net", tmp_path)
    backend = LocalExecutionBackend(tmp_path, session.get_cwd)
    from wright.application.permission.service import PermissionService
    from wright.infrastructure.config.permission_store import FilePermissionRepository

    permissions = PermissionService(FilePermissionRepository(tmp_path))
    snapshot = permissions.snapshot(session)
    change = resource_change(session, backend, snapshot, {"kind": "sandbox_network"}, "session")
    assert change.session_rules[0].kind == "sandbox_network"
    with pytest.raises(GrantError):
        resource_change(session, backend, snapshot, {"kind": "sandbox_network"}, "forever")


def test_http_prompt_is_unchanged_by_sandbox_network_choices():
    result = AuthorizationWorkflow().resolve(
        ToolCall("http_request", {"url": "https://example.com/api", "method": "GET"}, "http"),
        http_request_tool,
        backend=FixturePaths(),
        scope=AccessScope(Path("/project")),
        identity=InvocationIdentity("session"),
    )
    assert not any(choice.resource_kind == "sandbox_network" for choice in result.prompt.choices)
