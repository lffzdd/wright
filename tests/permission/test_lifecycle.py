"""Authoritative grant storage, version conflicts and execution boundaries."""


import pytest

from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.application.tool_execution.permissions import (
    PermissionConflict,
    PermissionService,
)
from wright.application.workspace.grants import list_grants, resource_change
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy import PermissionResolver, PermissionResponse
from wright.domain.policy.permission.types import AuthorizationChange
from wright.infrastructure.config.permission_store import (
    FilePermissionRepository,
    load_permission_settings,
)
from wright.infrastructure.persistence.session.codec import (
    _deserialize_session as deserialize_session,
)
from wright.infrastructure.persistence.session.codec import (
    _serialize_session as serialize_session,
)
from wright.infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from wright.infrastructure.tools.file import (
    edit_file_tool,
    read_file_tool,
    write_file_tool,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("WRIGHT_PERMISSION_CONFIG", raising=False)
    root = tmp_path / "workspace"
    root.mkdir()
    session = Session.create("permissions", root)
    service = PermissionService(FilePermissionRepository(root), execution_factory=AuthorizedExecution)
    backend = LocalExecutionBackend(root, session.get_cwd)
    return session, service, backend


def grant(setup, path, *, kind="file", lifetime="project", write=True):
    session, service, backend = setup
    snapshot = service.snapshot(session)
    change = resource_change(session, backend, snapshot, {"kind": kind, "path": str(path),
                            "operations": ["file_read", "file_write"] if write else ["file_read"]}, lifetime)
    service.commit(change, session, expected_version=snapshot.version)
    return list_grants(session, service)["grants"][-1]


def executor(setup, handler=None):
    session, _, _ = setup
    return ToolDispatchService({tool.name: tool for tool in (read_file_tool, write_file_tool, edit_file_tool)},
                               assemble_tool_capabilities(session, None, None), session=session,
                               permission_resolver=PermissionResolver(settings=load_permission_settings(), approval_handler=handler))


def write(dispatch, path, identity="call"):
    return dispatch.execute([ToolCall("write_file", {"file": str(path), "content": "written"}, identity)])[0].result


@pytest.mark.parametrize("lifetime", ["session", "project", "user"])
def test_remembered_file_is_shared_by_file_tools_but_never_siblings(setup, tmp_path, lifetime):
    session, permissions, _ = setup
    path = tmp_path / "external" / "a.txt"
    approvals = []

    def approve(request):
        approvals.append(request.prompt)
        return PermissionResponse(f"allow_{lifetime}_rule" if len(approvals) == 1 else "deny")

    dispatch = executor(setup, approve)
    assert write(dispatch, path).ok
    assert dispatch.execute([ToolCall("read_file", {"file": str(path)}, "read")])[0].result.ok
    assert dispatch.execute([ToolCall("edit_file", {"file": str(path), "old_text": "written", "new_text": "edited"}, "edit")])[0].result.ok
    assert len(approvals) == 1
    row = list_grants(session, permissions)["grants"][0]
    assert row["tool"] == "*" and row["lifetime"] == lifetime
    assert not write(dispatch, path.with_name("b.txt"), "sibling").ok
    assert not path.with_name("b.txt").exists()
    permissions.revoke(session, row["id"], lifetime, list_grants(session, permissions)["version"])
    assert not dispatch.execute([ToolCall("edit_file", {"file": str(path), "old_text": "edited", "new_text": "escaped"}, "revoked")])[0].result.ok
    assert path.read_text() == "edited"


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("lifetime", ["session", "project", "user"])
@pytest.mark.parametrize("write_access", [True, False])
def test_effective_policy_includes_resource_grants_with_their_actual_lifetime(setup, kind, lifetime, write_access):
    session, service, _ = setup
    path = session.workspace_dir / "nested" / ("a.txt" if kind == "file" else "directory")
    row = grant(setup, path, kind=kind, lifetime=lifetime, write=write_access)
    groups = {group["operation"]: group for group in list_grants(session, service)["effective_policy"]}
    read = next(rule for rule in groups["file_read"]["rules"] if rule["rule"]["id"] == row["id"])
    assert read["scope"] == lifetime
    assert read["target"] == str(path)
    assert read["resource_kind"] == kind
    assert read["tool"] == "*"
    assert any(rule["rule"]["id"] == row["id"] for rule in groups["file_write"]["rules"]) is write_access
    assert not any(rule["rule"]["id"] == row["id"] for rule in groups["shell"]["rules"])


def test_configured_restrictions_report_user_and_project_scope(setup):
    import json

    from wright.core.paths import user_permission_settings_path

    session, service, _ = setup
    service.snapshot(session)
    for path, tool in ((user_permission_settings_path(), "write_file"), (service.repository.project_path, "edit_file")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "permissions": {"deny": [tool]}}), encoding="utf-8")
    groups = {group["operation"]: group for group in list_grants(session, service)["effective_policy"]}
    scopes = {rule["tool"]: rule["scope"] for rule in groups["file_write"]["rules"] if rule["effect"] == "deny"}
    assert scopes == {"write_file": "user", "edit_file": "project"}


@pytest.mark.parametrize("lifetime", ["session", "project", "user"])
def test_file_grant_is_exact_and_revoked_once(setup, lifetime):
    session, service, _ = setup
    item = grant(setup, "nested/a.txt", lifetime=lifetime)
    dispatch = executor(setup)
    assert write(dispatch, "nested/a.txt").ok
    assert not write(dispatch, "nested/b.txt", "second").ok
    assert len(session.permission_rules) == (1 if lifetime == "session" else 0)
    listing = list_grants(session, service)
    service.revoke(session, item["id"], lifetime, listing["version"])
    assert list_grants(session, service)["grants"] == []
    assert not write(dispatch, "nested/a.txt", "after_revoke").ok


def test_once_has_no_reusable_authority(setup):
    session, service, _ = setup
    decisions = iter(["allow_once", "deny"])
    dispatch = executor(setup, lambda _: PermissionResponse(next(decisions)))
    assert write(dispatch, "once.txt").ok
    assert not write(dispatch, "once.txt", "again").ok
    assert list_grants(session, service)["grants"] == []


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypass", "plan"])
@pytest.mark.parametrize("lifetime", ["session", "project", "user"])
def test_external_read_only_is_hard_limit(setup, tmp_path, mode, lifetime):
    session, _, _ = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("readable")
    grant(setup, outside, kind="directory", lifetime=lifetime, write=False)
    session.permission_mode = mode
    dispatch = executor(setup, lambda _: PermissionResponse("allow_once"))
    assert dispatch.execute([ToolCall("read_file", {"file": str(outside / "a.txt")}, "read")])[0].result.ok
    assert not write(dispatch, outside / "a.txt").ok


def test_external_directory_read_write_is_reusable(setup, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    grant(setup, outside, kind="directory", write=True)
    dispatch = executor(setup)
    assert write(dispatch, outside / "a.txt").ok
    assert write(dispatch, outside / "nested" / "b.txt", "second").ok
    assert not write(dispatch, tmp_path / "unapproved.txt", "third").ok


def test_project_worktree_rules_rebase_to_execution_root(setup, tmp_path):
    session, service, _ = setup
    grant(setup, "a.txt")
    tree = tmp_path / "worktree"
    tree.mkdir()
    sibling = Session.create("worktree", tree, project_root=session.project_root)
    sibling_service = PermissionService(FilePermissionRepository(session.project_root))
    dispatch = executor((sibling, sibling_service, LocalExecutionBackend(tree, sibling.get_cwd)))
    assert write(dispatch, "a.txt").ok
    assert not write(dispatch, session.workspace_dir / "a.txt", "other_tree").ok
    unrelated = Session.create("other project", tmp_path / "other")
    assert PermissionService(FilePermissionRepository(unrelated.project_root)).snapshot(unrelated).settings.project_rules == []
    assert service.snapshot(session).settings.project_rules[0].project_id


def test_session_resume_preserves_authorization(setup):
    session, service, _ = setup
    grant(setup, "a.txt", lifetime="session")
    restored = deserialize_session(serialize_session(session))
    assert restored.permission_rules == session.permission_rules
    assert write(executor((restored, service, LocalExecutionBackend(restored.workspace_dir, restored.get_cwd))), "a.txt").ok


def test_conflict_is_detected_across_services(setup):
    session, service, _ = setup
    before = service.snapshot(session)
    item = grant(setup, "a.txt")
    other = PermissionService(FilePermissionRepository(session.project_root))
    assert other.snapshot(session).version != before.version
    with pytest.raises(PermissionConflict):
        other.revoke(session, item["id"], "project", before.version)
    with pytest.raises(PermissionConflict):
        service.commit(AuthorizationChange(), session, expected_version=before.version)


def test_revoke_during_approval_invalidates_answer(setup):
    session, service, _ = setup
    item = grant(setup, "previous.txt")
    def approve(_):
        service.revoke(session, item["id"], "project", service.snapshot(session).version)
        return PermissionResponse("allow_once")
    assert not write(executor(setup, approve), "waiting.txt").ok
    assert not (session.workspace_dir / "waiting.txt").exists()


def test_save_failure_rolls_back_only_this_commit(setup):
    session, service, backend = setup
    prior = grant(setup, "prior.txt")
    snapshot = service.snapshot(session)
    change = resource_change(session, backend, snapshot, {"kind": "file", "path": "new.txt", "operations": ["file_read"]}, "project")
    def fail(_):
        raise OSError("checkpoint failed")
    with pytest.raises(OSError):
        service.commit(change, session, fail, expected_version=snapshot.version)
    assert [row["id"] for row in list_grants(session, service)["grants"]] == [prior["id"]]


def test_live_child_tracks_parent_revocation(setup):
    parent, service, _ = setup
    item = grant(setup, "a.txt", lifetime="session")
    child = Session.create("child", parent.workspace_dir)
    child.permission_parent = parent
    child_setup = child, service, LocalExecutionBackend(child.workspace_dir, child.get_cwd)
    dispatch = executor(child_setup)
    assert write(dispatch, "a.txt").ok
    service.revoke(parent, item["id"], "session", service.snapshot(parent).version)
    assert not write(dispatch, "a.txt", "after_revoke").ok


def test_legacy_permission_data_is_rejected(setup):
    from wright.domain.policy import PermissionSettings
    from wright.infrastructure.persistence.session.errors import CheckpointError
    session, _, _ = setup
    with pytest.raises(ValueError, match="version"):
        PermissionSettings.from_dict({"permissions": {"allow": ["write_file"]}})
    data = serialize_session(session)
    data["session"]["permission_rules"] = ["write_file"]
    data["session"].pop("permission_data_version")
    with pytest.raises(CheckpointError):
        deserialize_session(data)


def test_http_deny_is_protocol_specific():
    from wright.domain.policy.permission.settings import MatchContext, PermissionRule
    rule = PermissionRule.parse("http_request(http://*)", effect="deny")
    assert rule.matches("http_request", "", effect="deny", context=MatchContext(url="http://example.com"))
    assert not rule.matches("http_request", "", effect="deny", context=MatchContext(url="https://example.com"))


def test_session_revoke_is_visible_to_another_runtime(setup):
    session, service, _backend = setup
    row = grant(setup, session.workspace_dir / "one", lifetime="session")
    reopened = deserialize_session(serialize_session(session))
    second = PermissionService(FilePermissionRepository(session.project_root or session.workspace_dir))
    snapshot = second.snapshot(reopened)
    assert snapshot.rules
    service.revoke(session, row["id"], "session", service.snapshot(session).version)
    assert second.snapshot(reopened).rules == ()
    with pytest.raises(PermissionConflict):
        second.validate(reopened, snapshot.version)


def test_reference_read_obeys_deny_and_protected_resources(setup):
    session, service, backend = setup
    secret = session.workspace_dir / "secret"
    secret.write_text("hidden", encoding="utf-8")
    from wright.infrastructure.config.permission_store import _update_settings
    _update_settings(lambda data: data["permissions"].update(deny=["read_file(secret)"]), None)
    with pytest.raises(PermissionError):
        service.read_file(session, backend, secret, max_bytes=100)


def test_reference_exact_file_uses_the_authorized_reader(setup, tmp_path):
    session, service, backend = setup
    path = tmp_path / "external.txt"
    path.write_text("allowed", encoding="utf-8")
    grant(setup, path, write=False)
    assert str(path) in service.reference_roots(session)
    assert service.read_file(session, backend, path, max_bytes=3) == b"all"


def test_shell_session_grant_is_bound_to_command_cwd_and_profile(setup, tmp_path):
    from dataclasses import replace

    from wright.domain.model.tool import ToolResult
    from wright.infrastructure.tools.command import execute_command_tool
    session, _service, _backend = setup
    offered = []
    def approve(request):
        offered.append(request.prompt)
        return PermissionResponse("allow_session_rule" if len(offered) == 1 else "deny")
    tool = replace(execute_command_tool, call=lambda *_: ToolResult.success())
    dispatch = ToolDispatchService({tool.name: tool}, assemble_tool_capabilities(session, None, None), session=session,
                                   permission_resolver=PermissionResolver(settings=load_permission_settings(), approval_handler=approve))
    assert dispatch.execute([ToolCall(tool.name, {"command": "echo hello"}, "one")])[0].result.ok
    assert dispatch.execute([ToolCall(tool.name, {"command": "echo hello"}, "two")])[0].result.ok
    assert len(offered) == 1
    assert not any(choice.lifetime in {"project", "user"} for choice in offered[0].choices)
    assert not dispatch.execute([ToolCall(tool.name, {"command": "echo changed"}, "three")])[0].result.ok
    assert len(offered) == 2
    extra = tmp_path / "extra"
    extra.mkdir()
    grant(setup, extra, kind="directory")
    assert not dispatch.execute([ToolCall(tool.name, {"command": "echo hello"}, "four")])[0].result.ok
    assert len(offered) == 3


def test_cross_runtime_revoke_cancels_pending_approval(setup):
    import threading
    import time

    from wright.application.session.interaction import RoutedPrompter
    from wright.application.tool_execution.approval import InteractiveApprovalHandler
    from wright.interfaces.interaction import InteractionBroker

    session, service, _ = setup
    row = grant(setup, "already-authorized.txt")
    events = []
    class Publisher:
        def publish(self, kind, payload):
            events.append((kind, payload))
    broker = InteractionBroker(Publisher())
    dispatch = executor(setup, InteractiveApprovalHandler(RoutedPrompter(broker)))
    results = []
    worker = threading.Thread(target=lambda: results.append(write(dispatch, "pending.txt")), daemon=True)
    worker.start()
    deadline = time.monotonic() + 3
    while not broker.snapshot():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    second = PermissionService(FilePermissionRepository(session.workspace_dir))
    second.revoke(session, row["id"], "project", service.snapshot(session).version)
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert not results[0].ok
    assert not (session.workspace_dir / "pending.txt").exists()
    assert broker.snapshot() == []
    assert any(kind == "interaction.resolved" for kind, _ in events)


def test_project_directory_cwd_resumes_and_revocation_snaps_back(setup, tmp_path):
    session, service, _ = setup
    directory = tmp_path / "external-directory"
    directory.mkdir()
    row = grant(setup, directory, kind="directory")
    session.set_cwd(directory)
    checkpoint = serialize_session(session)
    assert deserialize_session(checkpoint).get_cwd() == directory
    service.revoke(session, row["id"], "project", service.snapshot(session).version)
    assert deserialize_session(checkpoint).get_cwd() == session.workspace_dir


@pytest.mark.parametrize("field,value", [("permission_mode", "plan"), ("interaction_mode", "ask")])
def test_child_tracks_parent_hard_mode_changes(setup, field, value):
    parent, service, _ = setup
    child = Session.create("child", parent.workspace_dir)
    child.permission_parent = parent
    child.permission_mode = "bypass"
    child_setup = child, service, LocalExecutionBackend(child.workspace_dir, child.get_cwd)
    dispatch = executor(child_setup)
    old = service.snapshot(child)
    setattr(parent, field, value)
    with pytest.raises(PermissionConflict):
        service.validate(child, old.version)
    assert not write(dispatch, "child-write.txt").ok


def test_child_session_approval_belongs_to_resumable_conversation(setup):
    parent, service, _ = setup
    old_checkpoint = serialize_session(parent)
    child = Session.create("child", parent.workspace_dir)
    child.permission_parent = parent
    child_setup = child, service, LocalExecutionBackend(child.workspace_dir, child.get_cwd)
    grant(child_setup, "shared.txt", lifetime="session")
    assert child.permission_rules == []
    assert len(parent.permission_rules) == 1
    assert write(executor(setup), "shared.txt").ok
    resumed = deserialize_session(old_checkpoint)
    assert len(resumed.permission_rules) == 1
    row = list_grants(child, service)["grants"][0]
    service.revoke(child, row["id"], "session", service.snapshot(child).version)
    assert not write(executor(setup), "shared.txt").ok
