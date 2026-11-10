"""Boundaries that modes, grants and remembered rules must not cross."""

from pathlib import Path

import pytest
from scripts.generate_permission_fixtures import FixturePaths

from wright.application.composition.capabilities import assemble_tool_capabilities
from wright.application.permission.resources import resource_change
from wright.application.permission.service import PermissionService
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolAccess, ToolCall
from wright.domain.policy import PermissionResponse
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.settings import (
    MatchContext,
    PermissionRule,
    PermissionSettings,
)
from wright.domain.policy.permission.types import InvocationIdentity
from wright.infrastructure.config.permission_store import (
    FilePermissionRepository,
    load_permission_settings,
)
from wright.infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.file import read_file_tool, write_file_tool


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("WRIGHT_PERMISSION_CONFIG", raising=False)
    root = tmp_path / "workspace"
    root.mkdir()
    session = Session.create("boundaries", root)
    service = PermissionService(FilePermissionRepository(root), execution_factory=AuthorizedExecution)
    return session, service, LocalExecutionBackend(root, session.get_cwd)


def _grant_directory(setup, path, lifetime, *, write=False):
    session, service, backend = setup
    snapshot = service.snapshot(session)
    operations = ["file_read", "file_write"] if write else ["file_read"]
    change = resource_change(session, backend, snapshot, {"kind": "directory", "path": str(path), "operations": operations}, lifetime)
    service.commit(change, session, expected_version=snapshot.version)


def _mode(session, mode):
    session.authorization.set_modes(permission_mode=mode, interaction_mode=session.authorization.interaction_mode)


def _dispatch(setup, handler):
    session, _, _ = setup
    return ToolDispatchService({tool.name: tool for tool in (read_file_tool, write_file_tool)},
                               assemble_tool_capabilities(session, None, None), session=session,
                               permission_workflow=AuthorizationWorkflow(settings=load_permission_settings(), approval_handler=handler))


def _write(dispatch, path):
    return dispatch.execute([ToolCall("write", {"file": str(path), "content": "written"}, "write")])[0].result


def _recording(choice="deny"):
    asked = []

    def handler(request):
        asked.append(request)
        return PermissionResponse(choice)
    return asked, handler


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypass"])
def test_durable_automation_and_memory_writes_always_ask(mode):
    access = ToolAccess(frozenset({"persistent_write"}), subject="nightly", risk_flags=("persistent_automation",))
    decision = PermissionPolicy().evaluate(access, tool_name="create_schedule", subject="nightly", in_scope=True, permission_mode=mode)[0]
    assert decision == "ask"


def test_bypass_still_skips_confirmation_for_in_session_task_control():
    access = ToolAccess(frozenset({"execution_control"}))
    assert PermissionPolicy().evaluate(access, tool_name="stop_task", subject="", in_scope=True, permission_mode="bypass")[0] == "allow"


@pytest.mark.parametrize("lifetime", ["session", "project", "user"])
def test_read_only_grant_on_an_ancestor_keeps_the_workspace_writable(setup, tmp_path, lifetime):
    session, service, backend = setup
    _grant_directory(setup, tmp_path, lifetime)
    _mode(session, "acceptEdits")
    asked, handler = _recording()
    assert _write(_dispatch(setup, handler), session.workspace_dir / "a.txt").ok
    assert asked == []

    command = service.authorize(session, ToolCall("shell", {"command": "true"}, "shell"), execute_command_tool,
                                backend=backend, workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse("allow_once")))
    grant = command.resolution.grant
    assert str(session.workspace_dir) in {path.value for path in grant.shell_writable}
    assert str(tmp_path) in {path.value for path in grant.shell_readable}
    assert grant.shell_readonly == ()


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypass"])
def test_read_only_grant_reads_freely_but_writes_still_need_approval(setup, tmp_path, mode):
    session, _, _ = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("readable")
    _grant_directory(setup, outside, "project")
    _mode(session, mode)
    asked, handler = _recording()
    dispatch = _dispatch(setup, handler)
    assert dispatch.execute([ToolCall("read", {"file": str(outside / "a.txt")}, "read")])[0].result.ok
    assert asked == []
    assert not _write(dispatch, outside / "a.txt").ok
    assert len(asked) == 1
    assert any(choice.id == "allow_project_directory_write" for choice in asked[0].prompt.choices)
    assert _write(_dispatch(setup, lambda _: PermissionResponse("allow_once")), outside / "new.txt").ok


def test_read_write_grant_inside_a_read_only_grant_is_writable(setup, tmp_path):
    session, _, _ = setup
    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    _grant_directory(setup, outer, "session")
    _grant_directory(setup, outer / "inner", "session", write=True)
    _mode(session, "acceptEdits")
    asked, handler = _recording()
    dispatch = _dispatch(setup, handler)
    assert _write(dispatch, outer / "inner" / "a.txt").ok
    assert asked == []
    assert not _write(dispatch, outer / "b.txt").ok
    assert len(asked) == 1


@pytest.mark.parametrize("pattern,expected", [
    ("**/.env", {".env": True, "a/.env": True, "a/b/.env": True, "README.md": False, "a/.envrc": False}),
    ("src/**", {"src/a.py": True, "src/a/b.py": True, "README.md": False, "lib/src/a.py": False}),
    ("**/secrets/*.pem", {"secrets/k.pem": True, "a/b/secrets/k.pem": True, "a/secrets/sub/k.pem": False}),
])
def test_deny_rules_match_recursive_patterns(pattern, expected):
    rule = PermissionRule.parse(f"read({pattern})", effect="deny")
    for relative, denied in expected.items():
        context = MatchContext(session_origin="/w", files=((f"/w/{relative}", "file_read"),))
        assert rule.matches("read", relative, effect="deny", context=context) is denied, relative


def test_allow_rules_reject_recursive_patterns():
    record = {"kind": "file", "tool_name": "*", "root": "/w", "pattern": "**/a.txt", "operations": ["file_read"]}
    with pytest.raises(ValueError, match=r"\*\*"):
        PermissionSettings.from_dict({"version": 2, "permissions": {"allow": [record]}})


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypass"])
@pytest.mark.parametrize("name", [
    ".git/config", ".git/config.worktree", ".git/hooks/pre-commit",
    # Submodule gitdirs: Git reads their config when it enters the submodule.
    ".git/modules/sub/config", ".git/modules/lib/sub/config", ".git/modules/a/modules/b/config",
    ".git/modules/sub/config.worktree", ".git/modules/sub/hooks/pre-commit",
])
def test_git_control_files_need_a_fresh_approval_every_time(setup, mode, name):
    session, _, _ = setup
    (session.workspace_dir / ".git" / "hooks").mkdir(parents=True)
    (session.workspace_dir / name).parent.mkdir(parents=True, exist_ok=True)
    _grant_directory(setup, session.workspace_dir, "session", write=True)
    _mode(session, mode)
    asked, handler = _recording()
    assert not _write(_dispatch(setup, handler), session.workspace_dir / name).ok
    assert len(asked) == 1
    assert [choice.id for choice in asked[0].prompt.choices] == ["allow_once", "deny"]


def test_other_git_files_keep_normal_workspace_rules(setup):
    session, _, _ = setup
    (session.workspace_dir / ".git").mkdir()
    _mode(session, "acceptEdits")
    asked, handler = _recording()
    assert _write(_dispatch(setup, handler), session.workspace_dir / ".gitignore").ok
    assert asked == []


class _Paths(FixturePaths):
    def resolve_executable(self, program, *, cwd):
        return "/usr/bin/" + program


@pytest.mark.parametrize("execution_mode,offered", [("sandbox", True), ("host", False)])
def test_git_read_prefix_is_only_offered_inside_the_sandbox(execution_mode, offered):
    result = AuthorizationWorkflow(PermissionPolicy(), execution_mode=execution_mode).resolve(
        ToolCall("shell", {"command": "git status --short"}, "git"), execute_command_tool,
        backend=_Paths(), scope=AccessScope(Path("/project")), identity=InvocationIdentity("session"))
    ids = {choice.id for choice in result.prompt.choices}
    assert ("allow_project_command_prefix" in ids) is offered
    assert "allow_project_command_executable" in ids
