"""Independent acceptance for remembered authority at production boundaries."""
import shlex
from dataclasses import replace
from types import SimpleNamespace

import pytest

from wright.application.permission.service import PermissionConflict, PermissionService
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.application.workspace.grants import list_grants
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolAccess, ToolCall
from wright.domain.policy.permission.settings import MatchContext, PermissionRule
from wright.domain.policy.permission.types import PermissionResponse
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.knowledge import build_knowledge_tools
from wright.infrastructure.tools.web_tools import web_search_tool


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("WRIGHT_PERMISSION_CONFIG", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    session = Session.create("acceptance", root)
    service = PermissionService(FilePermissionRepository(root), execution_factory=AuthorizedExecution)
    return session, service, LocalExecutionBackend(root, session.get_cwd)


@pytest.mark.parametrize("lifetime", ["session", "project", "user"])
def test_program_memory_survives_repository_reload_then_revocation(boundary, lifetime):
    session, service, backend = boundary
    program = backend.resolve_executable("python3", cwd=backend.cwd())
    assert program
    prompts = []

    def approve(request):
        prompts.append(request.prompt)
        return PermissionResponse(f"allow_{lifetime}_command_executable")

    def invoke(command, workflow=None, target_service=service):
        return target_service.authorize(session, ToolCall("shell", {"command": command}, command),
            execute_command_tool, backend=backend, workflow=workflow, requires_execution=True)

    first = invoke("python3 -V", AuthorizationWorkflow(approval_handler=approve))
    assert first.execution is not None
    reloaded = PermissionService(FilePermissionRepository(session.project_root), execution_factory=AuthorizedExecution)
    second = invoke("python3 -c 'print(2)'", target_service=reloaded)
    assert second.execution is not None and len(prompts) == 1
    row = next(row for row in list_grants(session, reloaded)["grants"] if row["resource_kind"] == "command")
    reloaded.revoke(session, row["id"], lifetime, reloaded.snapshot(session).version)
    with pytest.raises(PermissionConflict, match="revoked"):
        second.execution.cwd()
    assert invoke("python3 -V", target_service=reloaded).execution is None


def test_launch_keeps_symlink_entry_and_rejects_retargeting(boundary, tmp_path, monkeypatch):
    session, service, backend = boundary
    target = tmp_path / "target"
    other = tmp_path / "other"
    for file in (target, other):
        file.write_text("#!/bin/sh\nexit 0\n")
        file.chmod(0o755)
    entry = session.workspace_dir / "python-link"
    entry.symlink_to(target)
    command = f"{shlex.quote(str(entry))} -V"
    invocation = service.authorize(session, ToolCall("shell", {"command": command}, "link"),
        execute_command_tool, backend=backend, requires_execution=True,
        workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse("allow_session_command_executable")))
    launched = []
    def launch(text, **kwargs):
        launched.append(text)
        return SimpleNamespace(group_alive=lambda: False)

    monkeypatch.setattr(backend, "start_shell", launch)
    invocation.execution.start_shell(command)
    assert shlex.split(launched[0])[0] == str(entry)
    entry.unlink()
    entry.symlink_to(other)
    with pytest.raises(PermissionError, match="program changed"):
        invocation.execution.start_shell(command)


@pytest.mark.parametrize("url,allowed", [
    ("https://example.com:8443/api?changed=1", True),
    ("https://example.com/api", False),
    ("http://example.com:8443/api", False),
    ("https://other.example.com:8443/api", False),
    ("https://example.com:8443/api/child", False),
    ("https://example.com:8443/API", False),
])
def test_exact_http_path_preserves_origin_port_and_case(url, allowed):
    rule = replace(PermissionRule.network_grant("http_request", "https", "example.com", 8443, ("GET",)),
        path="/api", path_match="exact")
    assert rule.matches("http_request", "", context=MatchContext(url=url, method="GET")) is allowed


def test_named_search_memory_cannot_authorize_persistent_writes(boundary):
    session, service, backend = boundary
    original = ToolCall("web_search", {"query": "first"}, "first")
    approved = service.authorize(session, original, web_search_tool, backend=backend,
        workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse("allow_project_rule")))
    assert approved.resolution.decision == "allow"
    changed = replace(web_search_tool, access_descriptor=lambda _: ToolAccess(frozenset({"network_read", "persistent_write"})))
    blocked = service.authorize(session, original, changed, backend=backend)
    assert blocked.resolution.decision == "deny"
    assert [choice.id for choice in blocked.resolution.prompt.choices] == ["allow_once", "deny"]


@pytest.mark.parametrize("permission_mode", ["default", "acceptEdits", "bypass", "plan"])
@pytest.mark.parametrize("interaction_mode", ["agent", "ask", "plan"])
def test_remembered_program_respects_existing_mode_ceilings(boundary, permission_mode, interaction_mode):
    session, service, backend = boundary
    command = ToolCall("shell", {"command": "python3 -V"}, "mode")
    service.authorize(session, command, execute_command_tool, backend=backend,
        workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse("allow_session_command_executable")))
    session.authorization.set_modes(permission_mode=permission_mode, interaction_mode=interaction_mode)
    result = service.authorize(session, command, execute_command_tool, backend=backend)
    allowed = permission_mode != "plan" and interaction_mode == "agent"
    assert result.resolution.decision == ("allow" if allowed else "deny")


@pytest.mark.parametrize("tool", [web_search_tool, build_knowledge_tools(object())[0]], ids=["web", "knowledge"])
def test_named_search_rule_is_revocable(boundary, tool):
    session, service, backend = boundary
    call = ToolCall(tool.name, {"query": "first"}, "search")
    approved = service.authorize(session, call, tool, backend=backend,
        workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse("allow_user_rule")))
    assert approved.resolution.decision == "allow"
    assert service.authorize(session, replace(call, arguments={"query": "different"}), tool,
        backend=backend).resolution.decision == "allow"
    row = next(row for row in list_grants(session, service)["grants"] if row["tool"] == tool.name)
    service.revoke(session, row["id"], "user", service.snapshot(session).version)
    assert service.authorize(session, call, tool, backend=backend).resolution.decision == "deny"
