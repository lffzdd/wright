"""Ownership, inheritance, and recovery regressions for session authorization."""

from dataclasses import FrozenInstanceError, replace

import pytest

from wright.application.permission.resources import resource_change
from wright.application.permission.service import PermissionConflict, PermissionService
from wright.application.workspace.modes import tool_visible
from wright.domain.model.session import Session, SessionAuthorization
from wright.domain.policy.permission.settings import PermissionRule
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.persistence.session.codec import (
    _deserialize_session,
    _serialize_session,
)
from wright.infrastructure.runtime import LocalExecutionBackend


def rule(root, identity="grant"):
    return PermissionRule(
        "*",
        kind="directory",
        root=str(root),
        operations=("file_read",),
        id=identity,
        recursive=True,
    )


def test_authorization_owns_immutable_rules_and_rejects_invalid_replacements(tmp_path):
    authorization = SessionAuthorization("owner", [rule(tmp_path)])
    before = authorization.rules
    with pytest.raises(FrozenInstanceError):
        before[0].root = "/other"
    with pytest.raises(TypeError):
        authorization.replace_rules([{}])
    with pytest.raises(ValueError, match="Duplicate"):
        authorization.replace_rules([*before, *before])
    with pytest.raises(ValueError, match="session lifetime"):
        authorization.add_rule(replace(before[0], lifetime="project"))
    assert authorization.rules == before
    assert authorization.revoke("grant")
    assert not authorization.revoke("grant")
    assert authorization.rules == ()


def test_parent_is_only_authorization_and_cycles_are_rejected():
    root = SessionAuthorization("root")
    child = SessionAuthorization("child")
    leaf = SessionAuthorization("leaf")
    child.bind_parent(root)
    leaf.bind_parent(child)
    assert leaf.owner() is root
    assert leaf.lineage() == (leaf, child, root)
    with pytest.raises(ValueError, match="cycle"):
        root.bind_parent(leaf)
    with pytest.raises(ValueError, match="cycle"):
        child.bind_parent(child)
    with pytest.raises(TypeError):
        child.bind_parent(object())
    assert root.lineage() == (root,)


def test_ancestor_mode_changes_also_change_child_tool_visibility(tmp_path):
    root = Session.create("root", tmp_path)
    child = Session.create("child", tmp_path)
    child.authorization.bind_parent(root.authorization)
    child.authorization.set_modes(permission_mode="bypass", interaction_mode="agent")
    root.authorization.set_modes(permission_mode="plan", interaction_mode="ask")
    assert child.authorization.effective_modes("default") == ("plan", "ask")
    assert not tool_visible("write_file", child)
    assert not tool_visible("update_plan", child)
    assert tool_visible("read_file", child)


def test_checkpoint_decoding_never_reads_permission_stores(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    session = Session.create("history", root)
    session.authorization.add_rule(rule(root))
    session.authorization.set_modes(permission_mode="plan", interaction_mode="ask")
    payload = _serialize_session(session)
    root.rmdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("History decoding must not read authorization stores")

    monkeypatch.setattr(FilePermissionRepository, "read", forbidden)
    monkeypatch.setattr(FilePermissionRepository, "read_session", forbidden)
    restored = _deserialize_session(payload)
    assert restored.authorization.snapshot() == session.authorization.snapshot()
    assert "authorization" in payload["session"]
    assert "permission_mode" not in payload["session"]


def test_mode_changes_are_atomic_and_checkpoint_failure_rolls_back(tmp_path):
    session = Session.create("modes", tmp_path)
    service = PermissionService(FilePermissionRepository(tmp_path))
    original = service.snapshot(session)
    with pytest.raises(ValueError):
        service.set_modes(session, interaction_mode="ask", permission_mode="bad")
    assert session.authorization.interaction_mode == "agent"

    def fail(_):
        raise OSError("checkpoint failed")

    from tests.permission_helpers import TestCheckpoint
    service.bind_checkpoint(session.session_id, TestCheckpoint(fail, session))
    with pytest.raises(OSError):
        service.set_modes(
            session,
            interaction_mode="plan",
            permission_mode="bypass",
        )
    assert service.snapshot(session).version == original.version
    service._checkpoints.clear()
    service.set_modes(session, interaction_mode="ask")
    with pytest.raises(PermissionConflict):
        service.validate(session, original.version)


def test_restore_uses_current_store_and_never_revives_revoked_grants(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    session = Session.create("restore", workspace)
    service = PermissionService(FilePermissionRepository(workspace))
    backend = LocalExecutionBackend(workspace, session.get_cwd)
    initial = service.snapshot(session)
    change = resource_change(
        session,
        backend,
        initial,
        {"kind": "directory", "path": str(external), "operations": ["file_read"]},
        "session",
    )
    service.commit(change, session, expected_version=initial.version)
    session.set_cwd(external)
    payload = _serialize_session(session)
    current = service.snapshot(session)
    service.revoke(session, current.rules[0].id, "session", current.version)
    restored = _deserialize_session(payload)
    assert restored.authorization.rules  # History reflects what was recorded.
    service.restore(restored, LocalExecutionBackend(workspace, restored.get_cwd))
    assert restored.authorization.rules == ()
    assert restored.get_cwd() == workspace


def test_three_lifetimes_keep_independent_ownership(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = Session.create("first", first_root)
    sibling = Session.create("sibling", first_root)
    other = Session.create("other", second_root)
    service = PermissionService(FilePermissionRepository(first_root))
    other_service = PermissionService(FilePermissionRepository(second_root))
    backend = LocalExecutionBackend(first_root, first.get_cwd)
    for lifetime in ("session", "project", "user"):
        snapshot = service.snapshot(first)
        change = resource_change(
            first,
            backend,
            snapshot,
            {
                "kind": "file",
                "path": str(first_root / f"{lifetime}.txt"),
                "operations": ["file_read"],
            },
            lifetime,
        )
        service.commit(change, first, expected_version=snapshot.version)
    own = service.snapshot(first)
    shared = service.snapshot(sibling)
    global_only = other_service.snapshot(other)
    assert len(own.rules) == 1
    assert shared.rules == global_only.rules == ()
    assert {
        rule.lifetime for rule in shared.settings.allow if rule.source == "approval"
    } == {"project", "user"}
    assert {
        rule.lifetime
        for rule in global_only.settings.allow
        if rule.source == "approval"
    } == {"user"}
    assert sibling.authorization.rules == other.authorization.rules == ()


def test_capability_reassembly_keeps_the_injected_permission_service(tmp_path):
    from wright.application.composition.capabilities import (
        assemble_tool_capabilities,
    )

    session = Session.create("run", tmp_path)
    permissions = PermissionService(FilePermissionRepository(tmp_path))
    assembly = assemble_tool_capabilities(session, None, None, permissions=permissions)
    session.begin_run("next", source="test")
    rebound = assemble_tool_capabilities(
        session,
        None,
        None,
        execution_backend=assembly.backend,
        permissions=assembly.permissions,
    )
    assert rebound.permissions is permissions
    assert rebound.capabilities.scope.run_id == session.active_run_id


def test_session_revoke_storage_failure_rolls_back_memory_and_store(
    tmp_path, monkeypatch
):
    session = Session.create("revoke", tmp_path)
    repository = FilePermissionRepository(tmp_path)
    service = PermissionService(repository)
    service.snapshot(session)
    session.authorization.add_rule(rule(tmp_path))
    repository.write_session(
        session.session_id,
        tuple(item.to_persistent() for item in session.authorization.rules),
    )
    snapshot = service.snapshot(session)
    original = repository.write_session
    failed = False

    def fail_once(identity, records):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("permission store failed")
        original(identity, records)

    monkeypatch.setattr(repository, "write_session", fail_once)
    with pytest.raises(OSError):
        service.revoke(session, "grant", "session", snapshot.version)
    assert session.authorization.rules == snapshot.rules
    assert service.snapshot(session).rules == snapshot.rules


def test_concurrent_parent_binding_cannot_create_a_cycle():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    left = SessionAuthorization("left")
    right = SessionAuthorization("right")
    barrier = threading.Barrier(2)

    def bind(child, parent):
        barrier.wait(timeout=2)
        try:
            child.bind_parent(parent)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bind, left, right)
        second = pool.submit(bind, right, left)
        assert sorted([first.result(timeout=2), second.result(timeout=2)]) == [
            False,
            True,
        ]
    assert left.owner() is right.owner()


def test_history_preview_does_not_rebuild_authorization_for_missing_execution_root(
    tmp_path, monkeypatch
):
    from wright.application.session.directory import SessionDirectory
    from wright.infrastructure.persistence.session.repository import (
        FileSessionRepository,
    )

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    session = Session.create(
        "history", workspace, project_root=tmp_path, environment="worktree"
    )
    store = FileSessionRepository(tmp_path / "checkpoints")
    store.save(session)
    workspace.rmdir()
    directory = SessionDirectory(tmp_path)
    directory.checkpoints = store

    def forbidden(*args, **kwargs):
        raise AssertionError("History preview must not refresh permission stores")

    monkeypatch.setattr(FilePermissionRepository, "read", forbidden)
    monkeypatch.setattr(FilePermissionRepository, "read_session", forbidden)
    preview = directory.preview(session.session_id)
    assert preview["view_only"]
    assert not preview["session"]["recoverable"]
    assert preview["session"]["permission_mode"] is None
