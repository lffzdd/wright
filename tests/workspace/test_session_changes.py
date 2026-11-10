"""Real-file/Git coverage for the session review scope and conservative undo."""

from __future__ import annotations

import json
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from wright.application.execution.directory import DirectoryExecutionCoordinator
from wright.application.session_runtime.errors import SessionServiceError
from wright.application.workspace.change_lock import (
    ChangeBusyError,
    workspace_change_lock,
)
from wright.application.workspace.journal import (
    ChangeJournalError,
    SessionChangeJournal,
)
from wright.application.workspace.session_commands import review_action
from wright.interfaces.web.auth import COOKIE_NAME, BootstrapAuth
from wright.interfaces.web.workspace_routes import add_workspace_routes


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "tests@example.test")
    git(root, "config", "user.name", "Tests")
    (root / "a.txt").write_bytes(b"base\n")
    (root / "b.txt").write_bytes(b"other\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    session = SimpleNamespace(session_id="review-session", project_root=root, workspace_dir=root)
    return root, session, SessionChangeJournal(session)


def edit(journal, root, path="a.txt", text="task\n", call="edit"):
    target = root / path
    existed = target.is_file()
    before = target.read_text() if existed else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    journal.note_text(target, before=before, after=text, existed=existed, call_id=call, tool_name="edit")


def service(session):
    runtime = SimpleNamespace(session_state=session, directory_coordinator=DirectoryExecutionCoordinator())
    return SimpleNamespace(runtime=runtime, session_id=session.session_id, summary=lambda: {"execution": "idle"})


def test_session_diff_uses_task_before_not_head_and_undo_preserves_index(workspace):
    root, _, journal = workspace
    (root / "a.txt").write_text("staged user change\n")
    git(root, "add", "a.txt")
    (root / "a.txt").write_text("unstaged user change\n")
    journal.ensure_baseline()
    index, head = git(root, "ls-files", "--stage"), git(root, "rev-parse", "HEAD")
    edit(journal, root, text="task change\n")
    result = journal.patch("a.txt")
    assert "-unstaged user change" in result["patch"] and "+task change" in result["patch"]
    assert "-base" not in result["patch"]
    assert (result["additions"], result["deletions"]) == (1, 1)
    assert journal.revert(["a.txt"], confirm=True, expected_revision=result["revision"])["applied"]
    assert (root / "a.txt").read_text() == "unstaged user change\n"
    assert git(root, "ls-files", "--stage") == index and git(root, "rev-parse", "HEAD") == head


def test_unowned_baseline_does_not_count_as_session_changes(workspace):
    root, _, journal = workspace
    (root / "a.txt").write_text("user\n")
    journal.ensure_baseline()
    result = journal.projection()
    assert result["changes"] == []
    assert result["unowned_changes"][0]["state"] == "preexisting"
    git(root, "add", ".")
    git(root, "commit", "-m", "user commit")
    assert journal.projection()["changes"] == []


def test_commit_changes_history_not_file_existence(workspace):
    root, session, journal = workspace
    edit(journal, root)
    revision = journal.projection()["revision"]
    git(root, "add", ".")
    git(root, "commit", "-m", "task")
    reopened = SessionChangeJournal(session)
    row = reopened.projection()["changes"][0]
    assert row["state"] == "task" and row["current_sha256"] == row["after_sha256"]
    assert row["history"] and row["committed"] and not row["can_revert"]
    assert "-base" in reopened.patch("a.txt")["patch"]
    with pytest.raises(ChangeJournalError, match="stale"):
        reopened.revert(["a.txt"], confirm=True, expected_revision=revision)
    assert reopened.revert(["a.txt"], confirm=True)["applied"] is False


def test_shell_capture_of_modified_and_committed_file_is_history(workspace):
    root, _, journal = workspace
    before = journal.capture()
    (root / "a.txt").write_text("command task\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "command commit")
    journal.commit_capture(before, call_id="command", tool_name="shell")
    row = journal.projection()["changes"][0]
    assert row["state"] == "task" and row["committed"] and not row["can_revert"]
    assert "+command task" in journal.patch("a.txt")["patch"]


def test_stale_revision_prevents_undo_after_external_edit(workspace):
    root, _, journal = workspace
    edit(journal, root)
    revision = journal.projection()["revision"]
    (root / "a.txt").write_text("user after task\n")
    with pytest.raises(ChangeJournalError, match="stale"):
        journal.revert(["a.txt"], confirm=True, expected_revision=revision)
    row = journal.projection()["changes"][0]
    assert row["state"] == "conflict" and not row["can_revert"]
    assert journal.revert(["a.txt"], confirm=True)["applied"] is False
    assert (root / "a.txt").read_text() == "user after task\n"


def test_task_after_external_interleaving_remains_conflicted(workspace):
    root, _, journal = workspace
    edit(journal, root)
    (root / "a.txt").write_text("interleaving user\n")
    edit(journal, root, text="second task\n", call="second")
    assert journal.projection()["changes"][0]["state"] == "conflict"
    assert not journal.revert(["a.txt"], confirm=True)["applied"]


def test_batch_conflict_preflight_changes_no_files(workspace):
    root, _, journal = workspace
    edit(journal, root)
    edit(journal, root, "b.txt", "task b\n")
    (root / "b.txt").write_text("external\n")
    assert not journal.revert(["a.txt", "b.txt"], confirm=True)["applied"]
    assert (root / "a.txt").read_text() == "task\n"


def test_batch_write_failure_rolls_back_previous_file_and_mode(workspace, monkeypatch):
    root, _, journal = workspace
    (root / "a.txt").chmod(0o755)
    edit(journal, root)
    edit(journal, root, "b.txt", "task b\n")
    original = journal._restore_before

    def fail_second(path):
        if path == "b.txt":
            raise OSError("disk write failed")
        original(path)

    monkeypatch.setattr(journal, "_restore_before", fail_second)
    result = journal.revert(["a.txt", "b.txt"], confirm=True)
    assert not result["applied"] and not result["rollback_errors"]
    assert (root / "a.txt").read_text() == "task\n"
    assert stat.S_IMODE((root / "a.txt").stat().st_mode) == 0o755
    assert len(journal.projection()["changes"]) == 2


def test_new_folder_has_file_patch_and_can_be_undone(workspace):
    root, _, journal = workspace
    before = journal.capture()
    (root / "new" / "deeper").mkdir(parents=True)
    (root / "new" / "deeper" / "created.txt").write_text("created\n")
    journal.commit_capture(before, call_id="mkdir", tool_name="shell")
    assert [row["path"] for row in journal.projection()["changes"]] == ["new/deeper/created.txt"]
    assert "+created" in journal.patch("new/deeper/created.txt")["patch"]
    assert journal.revert(["new/deeper/created.txt"], confirm=True)["applied"]
    assert not (root / "new" / "deeper" / "created.txt").exists()


def test_staged_rename_requires_pair_and_preserves_index(workspace):
    root, _, journal = workspace
    before = journal.capture()
    git(root, "mv", "a.txt", "renamed.txt")
    index = git(root, "ls-files", "--stage")
    journal.commit_capture(before, call_id="mv", tool_name="shell")
    rows = {row["path"]: row for row in journal.projection()["changes"]}
    assert rows["a.txt"]["rename_with"] == "renamed.txt"
    assert not journal.revert(["a.txt"], confirm=True)["applied"]
    assert journal.revert(["a.txt", "renamed.txt"], confirm=True)["applied"]
    assert (root / "a.txt").read_text() == "base\n" and not (root / "renamed.txt").exists()
    assert git(root, "ls-files", "--stage") == index


@pytest.mark.parametrize("payload, expected", [(b"a\x00b", "binary"), (b"\xffabc", "binary"), (b"x" * 1_000_001, "truncated")])
def test_binary_and_large_are_explicit_and_undo_is_disabled(workspace, payload, expected):
    root, _, journal = workspace
    before = journal.capture()
    (root / "new.bin").write_bytes(payload)
    journal.commit_capture(before, call_id="binary", tool_name="shell")
    row = journal.patch("new.bin")
    assert row[expected] and not row["patch"] and not row["can_revert"]
    assert not journal.revert(["new.bin"], confirm=True)["applied"]
    assert (root / "new.bin").read_bytes() == payload


def test_missing_snapshot_preflight_does_not_write_any_file(workspace):
    root, _, journal = workspace
    edit(journal, root)
    edit(journal, root, "b.txt", "task b\n")
    state = json.loads((journal.directory / "journal.json").read_text())
    (journal.directory / "blobs" / state["heads"]["b.txt"]["before_sha256"]).unlink()
    result = journal.revert(["a.txt", "b.txt"], confirm=True)
    assert not result["applied"]
    assert (root / "a.txt").read_text() == "task\n"


def test_symlink_replacement_is_blocked_even_when_bytes_match(workspace, tmp_path):
    root, _, journal = workspace
    edit(journal, root)
    outside = tmp_path / "outside"
    outside.write_text("task\n")
    (root / "a.txt").unlink()
    (root / "a.txt").symlink_to(outside)
    assert not journal.projection()["changes"][0]["can_revert"]
    assert "+task" in journal.patch("a.txt")["patch"]
    with pytest.raises(ChangeJournalError, match="symbolic"):
        journal.revert(["a.txt"], confirm=True)
    assert outside.read_text() == "task\n" and (root / "a.txt").is_symlink()


def test_mode_only_task_change_is_visible_and_reverted(workspace):
    root, _, journal = workspace
    before = journal.capture()
    (root / "a.txt").chmod(0o755)
    journal.commit_capture(before, call_id="chmod", tool_name="shell")
    row = journal.patch("a.txt")
    assert "old mode 100644" in row["patch"] and "new mode 100755" in row["patch"]
    assert row["can_revert"] and journal.revert(["a.txt"], confirm=True)["applied"]
    assert stat.S_IMODE((root / "a.txt").stat().st_mode) == 0o644


def test_journal_instances_do_not_lose_independent_record_updates(workspace):
    root, session, journal = workspace
    other = SessionChangeJournal(session)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(edit, journal, root, "a.txt", "task a\n")
        second = workers.submit(edit, other, root, "b.txt", "task b\n")
        first.result()
        second.result()
    assert {row["path"] for row in journal.projection()["changes"]} == {"a.txt", "b.txt"}


def test_root_lock_prevents_simultaneous_git_and_journal_mutations(workspace):
    root, _, journal = workspace
    edit(journal, root)
    with workspace_change_lock(root):
        with pytest.raises(ChangeBusyError):
            journal.revert(["a.txt"], confirm=True)
    assert (root / "a.txt").read_text() == "task\n"


def test_review_command_requires_revision_and_idle_directory(workspace):
    root, session, journal = workspace
    edit(journal, root)
    current = journal.projection()["revision"]
    command = service(session)
    with pytest.raises(SessionServiceError, match="expected_revision"):
        review_action(command, "revert", ["a.txt"], confirm=True)
    lease = command.runtime.directory_coordinator.acquire(root, kind="turn", holder_id="other", session_id="other", label="other")
    try:
        with pytest.raises(SessionServiceError, match="busy"):
            review_action(command, "revert", ["a.txt"], confirm=True, expected_revision=current)
    finally:
        lease.release()
    command.summary = lambda: {"execution": "running"}
    with pytest.raises(SessionServiceError, match="executing"):
        review_action(command, "revert", ["a.txt"], confirm=True, expected_revision=current)
    assert (root / "a.txt").read_text() == "task\n"


def test_legacy_journal_read_does_not_migrate_or_replace_history(workspace):
    root, _, journal = workspace
    edit(journal, root)
    path = journal.directory / "journal.json"
    data = json.loads(path.read_text())
    data.pop("version", None)
    for head in data["heads"].values():
        for field in ["recorded_head", "commit_base", "before_exists", "after_exists", "before_mode", "after_mode"]:
            head.pop(field, None)
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    assert journal.projection()["changes"][0]["state"] == "task"
    assert not journal.projection()["changes"][0]["can_revert"]
    assert "+task" in journal.patch("a.txt")["patch"]
    assert path.read_bytes() == original


def test_review_http_contract_patch_stale_and_required_revision(workspace):
    root, session, journal = workspace
    edit(journal, root)
    command = service(session)
    manager = SimpleNamespace(get=lambda _: SimpleNamespace(runtime=command.runtime, service=command))
    auth = BootstrapAuth("test-bootstrap")
    router = APIRouter(prefix="/api/v1")
    add_workspace_routes(router, manager, auth)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, auth.exchange("test-bootstrap"))
        prefix = f"/api/v1/sessions/{session.session_id}/review"
        view = client.get(prefix).json()
        patch = client.get(prefix + "/patch", params={"path": "a.txt"})
        assert patch.status_code == 200 and "+task" in patch.json()["patch"]
        assert patch.json()["revision"] == view["revision"]
        assert client.post(prefix + "/revert", json={"confirm": True, "paths": ["a.txt"]}).status_code == 422
        body = {"confirm": True, "paths": ["a.txt"], "expected_revision": "old"}
        assert client.post(prefix + "/revert", json=body).status_code == 409
        body["expected_revision"] = view["revision"]
        assert client.post(prefix + "/revert", json=body).json()["applied"]
        assert client.get(prefix).json()["changes"] == []
    assert (root / "a.txt").read_text() == "base\n"


def test_preflight_external_edit_never_restores_old_backup(workspace, monkeypatch):
    root, _, journal = workspace
    edit(journal, root)
    revision = journal.projection()["revision"]
    original_projection = journal.projection
    writes = []

    def projection_then_user_edit():
        result = original_projection()
        (root / "a.txt").write_text("user edit during preflight\n")
        return result

    monkeypatch.setattr(journal, "projection", projection_then_user_edit)
    monkeypatch.setattr(journal, "_write_bytes", lambda *args: writes.append(args))
    with pytest.raises(ChangeJournalError, match="preflight"):
        journal.revert(["a.txt"], confirm=True, expected_revision=revision)
    assert (root / "a.txt").read_text() == "user edit during preflight\n"
    assert writes == []


def test_rollback_does_not_overwrite_external_edit_after_own_write(workspace, monkeypatch):
    root, _, journal = workspace
    edit(journal, root)
    edit(journal, root, "b.txt", "task b\n")
    restore = journal._restore_before

    def user_edits_first_then_second_fails(path):
        if path == "b.txt":
            (root / "a.txt").write_text("user after first undo\n")
            raise OSError("second write failed")
        restore(path)

    monkeypatch.setattr(journal, "_restore_before", user_edits_first_then_second_fails)
    result = journal.revert(["a.txt", "b.txt"], confirm=True)
    assert not result["applied"]
    assert (root / "a.txt").read_text() == "user after first undo\n"
    assert (root / "b.txt").read_text() == "task b\n"
    assert any("latest contents were preserved" in error for error in result["rollback_errors"])
    assert next(row for row in result["results"] if row["path"] == "a.txt")["result"] == "rollback_conflict"
    assert len(journal.projection()["changes"]) == 2


def test_write_boundary_conflict_does_not_restore_unwritten_path(workspace, monkeypatch):
    root, _, journal = workspace
    edit(journal, root)
    edit(journal, root, "b.txt", "task b\n")
    restore = journal._restore_before

    def first_undo_then_user_changes_second(path):
        restore(path)
        if path == "a.txt":
            (root / "b.txt").write_text("user before second undo\n")

    monkeypatch.setattr(journal, "_restore_before", first_undo_then_user_changes_second)
    result = journal.revert(["a.txt", "b.txt"], confirm=True)
    assert not result["applied"] and not result["rollback_errors"]
    assert (root / "a.txt").read_text() == "task\n"
    assert (root / "b.txt").read_text() == "user before second undo\n"
    assert next(row for row in result["results"] if row["path"] == "b.txt")["result"] == "not_applied"


def test_existing_baseline_and_review_do_not_rewrite_journal(workspace, monkeypatch):
    root, _, journal = workspace
    edit(journal, root)
    original = (journal.directory / "journal.json").read_bytes()

    def unexpected_save(_):
        raise AssertionError("a read-only review rewrote the journal")

    monkeypatch.setattr(journal, "_save", unexpected_save)
    journal.ensure_baseline()
    assert journal.projection()["changes"][0]["current_sha256"]
    assert journal.patch("a.txt")["patch"]
    assert (journal.directory / "journal.json").read_bytes() == original


def test_user_edit_during_tempfile_preparation_is_preserved(workspace, monkeypatch):
    root, _, journal = workspace
    edit(journal, root)
    from wright.application.workspace import journal as journal_module

    mkstemp = journal_module.tempfile.mkstemp

    def prepare_then_external_edit(*args, **kwargs):
        result = mkstemp(*args, **kwargs)
        if kwargs.get("prefix") == ".wright-undo-":
            (root / "a.txt").write_text("user while undo prepared\n")
        return result

    monkeypatch.setattr(journal_module.tempfile, "mkstemp", prepare_then_external_edit)
    result = journal.revert(["a.txt"], confirm=True)
    assert not result["applied"]
    assert (root / "a.txt").read_text() == "user while undo prepared\n"
    assert result["results"][0]["result"] == "rollback_conflict"
    assert not list(root.glob(".wright-undo-*"))


def test_rollback_rechecks_user_edit_at_its_write_boundary(workspace, monkeypatch):
    root, _, journal = workspace
    edit(journal, root)
    edit(journal, root, "b.txt", "task b\n")
    restore, write = journal._restore_before, journal._write_bytes
    writes = 0

    def fail_second(path):
        if path == "b.txt":
            raise OSError("second write failed")
        restore(path)

    def write_then_edit_before_rollback(target, data, mode, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            (root / "a.txt").write_text("user at rollback boundary\n")
        return write(target, data, mode, **kwargs)

    monkeypatch.setattr(journal, "_restore_before", fail_second)
    monkeypatch.setattr(journal, "_write_bytes", write_then_edit_before_rollback)
    result = journal.revert(["a.txt", "b.txt"], confirm=True)
    assert not result["applied"]
    assert (root / "a.txt").read_text() == "user at rollback boundary\n"
    assert (root / "b.txt").read_text() == "task b\n"
    assert any("later edits were preserved" in error for error in result["rollback_errors"])


def test_later_task_write_cannot_reactivate_undo_across_commit(workspace):
    root, _, journal = workspace
    edit(journal, root, text="committed task A\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-m", "task A")
    edit(journal, root, text="uncommitted task B\n", call="task B")
    view = journal.patch("a.txt")
    assert view["contains_committed_edits"] and view["history"]
    assert not view["committed"] and not view["can_revert"]
    assert "-base" in view["patch"] and "+uncommitted task B" in view["patch"]
    assert not journal.revert(["a.txt"], confirm=True, expected_revision=view["revision"])["applied"]
    assert (root / "a.txt").read_text() == "uncommitted task B\n"
    assert git(root, "show", "HEAD:a.txt") == b"committed task A\n"


def test_commit_of_staged_earlier_task_write_blocks_cumulative_undo(workspace):
    root, _, journal = workspace
    edit(journal, root, text="task staged A\n")
    git(root, "add", "a.txt")
    edit(journal, root, text="task unstaged B\n", call="task B")
    git(root, "commit", "-m", "commit earlier staged task A")
    row = journal.projection()["changes"][0]
    assert row["state"] == "task" and row["history"] and not row["committed"]
    assert not row["can_revert"]
    assert not journal.revert(["a.txt"], confirm=True)["applied"]
    assert (root / "a.txt").read_text() == "task unstaged B\n"
    assert git(root, "show", "HEAD:a.txt") == b"task staged A\n"


def test_unrelated_commit_does_not_block_safe_task_undo(workspace):
    root, _, journal = workspace
    edit(journal, root)
    (root / "b.txt").write_text("unrelated commit\n")
    git(root, "add", "b.txt")
    git(root, "commit", "-m", "unrelated")
    row = journal.projection()["changes"][0]
    assert row["state"] == "task" and not row["history"] and row["can_revert"]
    assert journal.revert(["a.txt"], confirm=True)["applied"]
    assert (root / "a.txt").read_text() == "base\n"
    assert git(root, "show", "HEAD:b.txt") == b"unrelated commit\n"


def test_first_git_commit_of_earlier_task_snapshot_blocks_cumulative_undo(workspace, tmp_path):
    _, _, _journal = workspace
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "tests@example.test")
    git(root, "config", "user.name", "Tests")
    session = SimpleNamespace(session_id="first-commit", project_root=root, workspace_dir=root)
    journal = SessionChangeJournal(session)
    edit(journal, root, text="first staged task A\n")
    git(root, "add", "a.txt")
    edit(journal, root, text="unstaged task B\n", call="B")
    git(root, "commit", "-m", "first commit of A")
    row = journal.projection()["changes"][0]
    assert row["history"] and not row["can_revert"]
    assert not journal.revert(["a.txt"], confirm=True)["applied"]
    assert (root / "a.txt").read_text() == "unstaged task B\n"


@pytest.mark.parametrize("session_uses_alias", [True, False])
def test_workspace_root_alias_records_real_task_snapshots(workspace, tmp_path, session_uses_alias):
    root, _, _journal = workspace
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(root, target_is_directory=True)
    session = SimpleNamespace(session_id="alias-session", project_root=root, workspace_dir=alias if session_uses_alias else root)
    journal = SessionChangeJournal(session)
    target = alias / "a.txt"
    before = target.read_text()
    target.write_text("task through workspace alias\n")
    journal.note_text(target, before=before, after="task through workspace alias\n", existed=True, call_id="alias-edit", tool_name="edit")
    row = journal.projection()["changes"][0]
    assert row["path"] == "a.txt" and row["can_revert"]
    assert "+task through workspace alias" in journal.patch("a.txt")["patch"]
    assert journal.revert(["a.txt"], confirm=True)["applied"]
    assert target.read_text() == "base\n"


@pytest.mark.parametrize("points_outside", [True, False])
def test_parent_symlink_inside_workspace_is_not_hidden_by_alias_resolution(workspace, tmp_path, points_outside):
    root, _, journal = workspace
    destination = tmp_path / "outside" if points_outside else root / "inside"
    destination.mkdir()
    (destination / "file.txt").write_text("before\n")
    (root / "linked").symlink_to(destination, target_is_directory=True)
    target = root / "linked" / "file.txt"
    target.write_text("after\n")
    journal.note_text(target, before="before\n", after="after\n", existed=True, call_id="symlink-parent", tool_name="edit")
    assert journal.projection()["changes"] == []
    assert (destination / "file.txt").read_text() == "after\n"


def test_internal_symlink_back_to_workspace_root_is_not_an_alias_root(workspace):
    root, _, journal = workspace
    (root / "loop").symlink_to(root, target_is_directory=True)
    target = root / "loop" / "a.txt"
    target.write_text("task through internal loop\n")
    journal.note_text(target, before="base\n", after="task through internal loop\n", existed=True, call_id="loop-edit", tool_name="edit")
    assert journal.projection()["changes"] == []


def test_leaf_symlink_is_not_resolved_to_claim_target_as_task_write(workspace):
    root, _, journal = workspace
    (root / "leaf.txt").symlink_to(root / "a.txt")
    target = root / "leaf.txt"
    target.write_text("task through leaf symlink\n")
    journal.note_text(target, before="base\n", after="task through leaf symlink\n", existed=True, call_id="leaf-edit", tool_name="edit")
    assert journal.projection()["changes"] == []


@pytest.mark.parametrize("change_kind", ["modify", "delete"])
def test_subdirectory_workspace_committed_state_uses_workspace_relative_object_path(workspace, change_kind):
    root, _, _journal = workspace
    sub = root / "sub"
    sub.mkdir()
    (root / "inside.txt").write_text("outside workspace, same filename\n")
    (sub / "inside.txt").write_text("subdirectory base\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "subdirectory fixture")
    journal = SessionChangeJournal(SimpleNamespace(session_id="subdirectory-review", project_root=sub, workspace_dir=sub))
    if change_kind == "modify":
        edit(journal, sub, path="inside.txt", text="subdirectory task\n")
    else:
        before = journal.capture()
        (sub / "inside.txt").unlink()
        journal.commit_capture(before, call_id="delete", tool_name="shell")
    assert journal.projection()["changes"][0]["can_revert"]
    git(sub, "add", "inside.txt")
    git(sub, "commit", "-m", "commit subdirectory task")
    view = journal.projection()
    assert [row["path"] for row in view["changes"]] == ["inside.txt"]
    row = view["changes"][0]
    assert row["state"] == "task" and row["committed"] and row["history"]
    assert not row["can_revert"]
    assert "-subdirectory base" in journal.patch("inside.txt")["patch"]
    assert (root / "inside.txt").read_text() == "outside workspace, same filename\n"


def test_session_rename_is_one_logical_patch_with_zero_text_statistics(workspace):
    root, _, journal = workspace
    (root / "a.txt").write_text("user content before task\n")
    git(root, "add", "a.txt")
    index, head = git(root, "ls-files", "--stage"), git(root, "rev-parse", "HEAD")
    before = journal.capture()
    (root / "a.txt").rename(root / "renamed.txt")
    journal.commit_capture(before, call_id="rename", tool_name="shell")
    view = journal.projection()
    rows = {row["path"]: row for row in view["changes"]}
    destination = rows["renamed.txt"]
    assert destination["display_kind"] == "rename" and destination["old_path"] == "a.txt"
    assert (destination["additions"], destination["deletions"]) == (0, 0)
    assert rows["a.txt"]["can_revert"] and destination["can_revert"]
    patch = journal.patch("renamed.txt")
    assert patch["old_path"] == "a.txt" and patch["new_path"] == "renamed.txt"
    assert "diff --git a/a.txt b/renamed.txt\n" in patch["patch"]
    assert "similarity index 100%\nrename from a.txt\nrename to renamed.txt\n" in patch["patch"]
    assert "/dev/null" not in patch["patch"] and "+user content" not in patch["patch"]
    assert journal.patch("a.txt")["patch"] == patch["patch"]
    assert journal.revert(["a.txt", "renamed.txt"], confirm=True, expected_revision=view["revision"])["applied"]
    assert (root / "a.txt").read_text() == "user content before task\n"
    assert not (root / "renamed.txt").exists()
    assert git(root, "ls-files", "--stage") == index and git(root, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("changed_path", ["a.txt", "renamed.txt"])
def test_session_rename_pair_shares_external_conflict_and_undo_eligibility(workspace, changed_path):
    root, _, journal = workspace
    before = journal.capture()
    (root / "a.txt").rename(root / "renamed.txt")
    journal.commit_capture(before, call_id="rename", tool_name="shell")
    (root / changed_path).write_text("external after rename\n")
    rows = journal.projection()["changes"]
    assert len(rows) == 2
    assert all(row["state"] == "conflict" and not row["can_revert"] for row in rows)
    patch = journal.patch("renamed.txt")
    assert "rename from a.txt" in patch["patch"] and patch["state"] == "conflict"
    assert not journal.revert(["a.txt", "renamed.txt"], confirm=True)["applied"]
    assert (root / changed_path).read_text() == "external after rename\n"
