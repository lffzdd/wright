"""Independent Changes acceptance against HTTP routes and real temporary Git trees.

The session handle is deliberately small: no model runs or user checkout is used.
Git, the change journal, directory coordination and HTTP handlers are real.
"""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wright.application.execution.directory import DirectoryExecutionCoordinator
from wright.application.workspace.journal import SessionChangeJournal
from wright.infrastructure.workspace.project import ProjectContext
from wright.interfaces.web.auth import BootstrapAuth
from wright.interfaces.web.server import create_app


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
    ).stdout


def facts(root: Path) -> dict:
    """Compare user-visible data, not Git's opportunistic index stat cache."""
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "index": git(root, "ls-files", "--stage", "-z"),
        "staged": git(root, "diff", "--cached", "--binary", "HEAD", "--"),
        "files": {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if ".git" not in path.relative_to(root).parts and path.is_file()
        },
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "wright-home"))
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "changes@example.test")
    git(root, "config", "user.name", "Changes Acceptance")
    git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    (root / "b.txt").write_text("unrelated\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    state = SimpleNamespace(session_id="changes-test", project_root=root, workspace_dir=root)
    runtime = SimpleNamespace(
        session_state=state,
        project_context=ProjectContext.local(root),
        directory_coordinator=DirectoryExecutionCoordinator(),
    )
    service = SimpleNamespace(
        session_id=state.session_id, runtime=runtime, execution="idle",
    )
    service.summary = lambda: {"execution": service.execution}
    handle = SimpleNamespace(runtime=runtime, service=service)
    manager = SimpleNamespace(get=lambda _session_id: handle, shutdown=lambda: None)
    app = create_app(manager, BootstrapAuth("test-bootstrap"), static_dir=tmp_path / "no-static")
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/auth/exchange", json={"token": "test-bootstrap"},
            headers={"origin": "http://testserver"},
        )
        assert result.status_code == 200
        yield SimpleNamespace(
            root=root, state=state, runtime=runtime, service=service, client=client,
            base=f"/api/v1/sessions/{state.session_id}",
            journal=SessionChangeJournal(state),
        )


def view(env, scope="working"):
    response = env.client.get(f"{env.base}/changes", params={"scope": scope})
    assert response.status_code == 200, response.text
    return response.json()


def patch(env, path="a.txt", scope="working"):
    response = env.client.get(f"{env.base}/changes/{path}", params={"scope": scope})
    assert response.status_code == 200, response.text
    return response.json()


def action(env, action_name, scope="working", paths=None, **extra):
    revision = extra.pop("expected_revision", None)
    if revision is None:
        revision = view(env, scope)["revision"]
    return env.client.post(
        f"{env.base}/changes/actions",
        json={
            "action": action_name, "scope": scope, "paths": paths or [],
            "expected_revision": revision, **extra,
        },
        headers={"origin": "http://testserver"},
    )


def succeeded(response):
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True, response.text


def note(env, name: str, text: str):
    path = env.root / name
    before = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(text, encoding="utf-8")
    env.journal.note_text(
        path, before=before, after=text, existed=before is not None,
        call_id=f"write-{name}", tool_name="edit_file",
    )


def review(env):
    response = env.client.get(f"{env.base}/review")
    assert response.status_code == 200, response.text
    return response.json()


def undo(env, paths, revision):
    return env.client.post(
        f"{env.base}/review/revert",
        json={"confirm": True, "paths": paths, "expected_revision": revision},
        headers={"origin": "http://testserver"},
    )


def test_scopes_stage_and_unstage_keep_disk_bytes(env):
    (env.root / "a.txt").write_text("staged user bytes\n", encoding="utf-8")
    git(env.root, "add", "a.txt")
    (env.root / "a.txt").write_text("working user bytes\n", encoding="utf-8")
    before = facts(env.root)
    assert "+staged user bytes" in patch(env, scope="staged")["patch"]
    unstaged = patch(env, scope="unstaged")["patch"]
    assert "-staged user bytes" in unstaged and "+working user bytes" in unstaged
    working = patch(env)["patch"]
    assert "-base" in working and "+working user bytes" in working
    assert facts(env.root) == before
    succeeded(action(env, "stage", scope="unstaged", paths=["a.txt"]))
    assert git(env.root, "show", ":a.txt") == b"working user bytes\n"
    assert facts(env.root)["files"] == before["files"]
    succeeded(action(env, "unstage", scope="staged", paths=["a.txt"]))
    assert git(env.root, "show", ":a.txt") == b"base\n"
    assert facts(env.root)["files"] == before["files"]
    assert facts(env.root)["head"] == before["head"]


@pytest.mark.parametrize("working_bytes", [b"binary\0working bytes", b"x" * (1024 * 1024 + 1)], ids=["binary-disk", "large-disk"])
def test_staged_diff_metadata_uses_index_content_instead_of_disk(env, working_bytes):
    (env.root / "a.txt").write_bytes(b"staged text\n")
    git(env.root, "add", "a.txt")
    (env.root / "a.txt").write_bytes(working_bytes)
    before = facts(env.root)
    result = patch(env, scope="staged")
    assert result["binary"] is False and result["truncated"] is False
    assert "+staged text" in result["patch"] and "-base" in result["patch"]
    assert result["size"] == len(b"staged text\n")
    assert facts(env.root) == before


def test_discard_unstaged_restores_index_without_losing_staged_edits(env):
    (env.root / "a.txt").write_text("staged\n", encoding="utf-8")
    git(env.root, "add", "a.txt")
    (env.root / "a.txt").write_text("working\n", encoding="utf-8")
    before = facts(env.root)
    succeeded(action(env, "discard", scope="unstaged", paths=["a.txt"], confirm=True))
    after = facts(env.root)
    assert after["files"]["a.txt"] == b"staged\n"
    assert after["files"]["b.txt"] == before["files"]["b.txt"]
    assert after["index"] == before["index"]
    assert after["staged"] == before["staged"]
    assert after["head"] == before["head"]


@pytest.mark.parametrize("operation", ["stage", "unstage", "discard", "commit"])
def test_stale_git_actions_reject_without_any_partial_mutation(env, operation):
    scope = "staged" if operation in {"unstage", "commit"} else "working"
    (env.root / "a.txt").write_text("first\n", encoding="utf-8")
    (env.root / "b.txt").write_text("second\n", encoding="utf-8")
    if scope == "staged":
        git(env.root, "add", "a.txt", "b.txt")
    stale = view(env, scope)["revision"]
    (env.root / "a.txt").write_text("later user edit\n", encoding="utf-8")
    if scope == "staged":
        git(env.root, "add", "a.txt")
    before = facts(env.root)
    response = action(
        env, operation, scope=scope, paths=["a.txt", "b.txt"],
        expected_revision=stale, confirm=True, message="must not commit stale index",
    )
    assert response.status_code == 409, response.text
    assert facts(env.root) == before


def test_hunk_stage_unstage_and_discard_only_touch_the_selected_hunk(env):
    original = [f"line-{index}\n" for index in range(1, 45)]
    (env.root / "a.txt").write_text("".join(original), encoding="utf-8")
    git(env.root, "add", "a.txt")
    git(env.root, "commit", "-m", "many lines")
    edited = original.copy()
    edited[1] = "first edit\n"
    edited[38] = "second edit\n"
    (env.root / "a.txt").write_text("".join(edited), encoding="utf-8")
    before = facts(env.root)
    assert sum(line.startswith("@@") for line in patch(env, scope="unstaged")["patch"].splitlines()) == 2
    succeeded(action(env, "stage", scope="unstaged", paths=["a.txt"], hunk=0))
    staged_bytes = git(env.root, "show", ":a.txt")
    assert b"first edit\n" in staged_bytes and b"second edit\n" not in staged_bytes
    assert facts(env.root)["files"] == before["files"]
    succeeded(action(env, "unstage", scope="staged", paths=["a.txt"], hunk=0))
    assert git(env.root, "show", ":a.txt") == "".join(original).encode()
    assert facts(env.root)["files"] == before["files"]
    succeeded(action(env, "discard", scope="unstaged", paths=["a.txt"], hunk=0, confirm=True))
    wanted = original.copy()
    wanted[38] = "second edit\n"
    assert (env.root / "a.txt").read_bytes() == "".join(wanted).encode()
    assert git(env.root, "show", ":a.txt") == "".join(original).encode()
    assert facts(env.root)["head"] == before["head"]


def test_new_directory_lists_each_file_and_staged_rename_keeps_both_paths(env):
    directory = env.root / "新目录 with spaces"
    directory.mkdir()
    (directory / "one.txt").write_text("new one\n", encoding="utf-8")
    (directory / "two.txt").write_text("new two\n", encoding="utf-8")
    paths = {row["path"] for row in view(env)["changes"]}
    assert paths == {"新目录 with spaces/one.txt", "新目录 with spaces/two.txt"}
    for path in paths:
        assert "+new " in patch(env, path)["patch"]
    git(env.root, "mv", "a.txt", "renamed.txt")
    rows = view(env)["changes"]
    rename = next(row for row in rows if row["path"] == "renamed.txt")
    assert rename["old_path"] == "a.txt"
    rename_patch = patch(env, "renamed.txt")["patch"]
    assert "rename from a.txt" in rename_patch and "rename to renamed.txt" in rename_patch


def test_commit_uses_existing_index_and_keeps_unstaged_and_untracked_files(env):
    (env.root / "a.txt").write_text("staged only\n", encoding="utf-8")
    git(env.root, "add", "a.txt")
    (env.root / "a.txt").write_text("later working bytes\n", encoding="utf-8")
    (env.root / "b.txt").write_text("other user edits\n", encoding="utf-8")
    (env.root / "new.txt").write_text("untracked user file\n", encoding="utf-8")
    before = facts(env.root)
    succeeded(action(env, "commit", scope="staged", confirm=True, message="selected staged edit"))
    after = facts(env.root)
    assert after["head"] != before["head"]
    assert git(env.root, "show", "HEAD:a.txt") == b"staged only\n"
    assert git(env.root, "show", "HEAD:b.txt") == b"unrelated\n"
    assert after["files"] == before["files"]
    assert not git(env.root, "diff", "--cached", "--name-only")
    assert git(env.root, "log", "-1", "--format=%s").strip() == b"selected staged edit"


def test_busy_session_and_other_directory_worker_block_git_mutations(env):
    (env.root / "a.txt").write_text("task\n", encoding="utf-8")
    revision = view(env)["revision"]
    before = facts(env.root)
    env.service.execution = "running"
    response = action(env, "stage", paths=["a.txt"], expected_revision=revision)
    assert response.status_code == 409, response.text
    assert facts(env.root) == before
    env.service.execution = "idle"
    lease = env.runtime.directory_coordinator.acquire(
        env.root, kind="turn", holder_id="other-turn", session_id="another-session", label="another task",
    )
    try:
        response = action(env, "stage", paths=["a.txt"], expected_revision=revision)
        assert response.status_code == 409, response.text
        assert facts(env.root) == before
    finally:
        lease.release()


def test_session_undo_restores_dirty_before_bytes_and_preserves_index_head(env):
    (env.root / "a.txt").write_text("staged user edit\n", encoding="utf-8")
    git(env.root, "add", "a.txt")
    (env.root / "a.txt").write_text("dirty before task\n", encoding="utf-8")
    (env.root / "b.txt").write_text("unrelated user edit\n", encoding="utf-8")
    env.journal.ensure_baseline()
    before_task = facts(env.root)
    note(env, "a.txt", "task result\n")
    session_patch = env.client.get(f"{env.base}/review/patch", params={"path": "a.txt"})
    assert session_patch.status_code == 200, session_patch.text
    assert "-dirty before task" in session_patch.json()["patch"]
    assert "+task result" in session_patch.json()["patch"]
    succeeded(undo(env, ["a.txt"], review(env)["revision"]))
    assert facts(env.root) == before_task


def test_session_undo_preflights_external_edit_for_whole_batch(env):
    env.journal.ensure_baseline()
    note(env, "a.txt", "task A\n")
    note(env, "b.txt", "task B\n")
    stale = review(env)["revision"]
    (env.root / "a.txt").write_text("later external edit\n", encoding="utf-8")
    before = facts(env.root)
    response = undo(env, ["b.txt", "a.txt"], stale)
    assert response.status_code in {400, 409}, response.text
    assert facts(env.root) == before
    current = review(env)
    external = next(row for row in current["changes"] if row["path"] == "a.txt")
    assert external["can_revert"] is False
    response = undo(env, ["b.txt", "a.txt"], current["revision"])
    assert response.status_code >= 400 or response.json().get("applied") is False
    assert facts(env.root) == before


def test_journal_reopen_and_git_commit_keep_task_history_without_false_conflict(env):
    env.journal.ensure_baseline()
    note(env, "a.txt", "task result\n")
    original = review(env)
    reopened = SessionChangeJournal(env.state)
    assert reopened.projection()["revision"] == original["revision"]
    git(env.root, "add", "a.txt")
    git(env.root, "commit", "-m", "commit task")
    assert view(env)["changes"] == []
    task = next(row for row in review(env)["changes"] if row["path"] == "a.txt")
    assert task["state"] != "conflict"
    assert task["current_sha256"] is not None
    assert task["committed"] is True and task["can_revert"] is False
    response = env.client.get(f"{env.base}/review/patch", params={"path": "a.txt"})
    assert response.status_code == 200, response.text
    assert "-base" in response.json()["patch"] and "+task result" in response.json()["patch"]


@pytest.mark.parametrize("operation", ["discard", "commit"])
def test_destructive_actions_require_confirmation_and_preserve_everything(env, operation):
    (env.root / "a.txt").write_text("user change\n", encoding="utf-8")
    git(env.root, "add", "a.txt")
    before = facts(env.root)
    response = action(
        env, operation, scope="staged" if operation == "commit" else "working",
        paths=["a.txt"], message="user message",
    )
    assert response.status_code in {400, 409, 422}, response.text
    assert facts(env.root) == before


def test_batch_invalid_path_never_stages_the_valid_first_path(env):
    (env.root / "a.txt").write_text("user change\n", encoding="utf-8")
    outside = env.root.parent / "outside.txt"
    outside.write_bytes(b"outside must survive\n")
    before = facts(env.root)
    response = action(env, "stage", paths=["a.txt", "../outside.txt"])
    assert response.status_code in {400, 409, 422}, response.text
    assert facts(env.root) == before
    assert outside.read_bytes() == b"outside must survive\n"


def test_index_change_invalidates_working_scope_revision_even_when_disk_is_identical(env):
    (env.root / "a.txt").write_text("user change\n", encoding="utf-8")
    revision = view(env)["revision"]
    git(env.root, "add", "a.txt")
    before = facts(env.root)
    response = action(env, "discard", paths=["a.txt"], expected_revision=revision, confirm=True)
    assert response.status_code == 409, response.text
    assert facts(env.root) == before


def test_failed_stage_does_not_rollback_an_external_git_index_write(env, monkeypatch):
    from wright.application.workspace import changes

    (env.root / "a.txt").write_bytes(b"requested stage\n")
    (env.root / "b.txt").write_bytes(b"external staged edit\n")
    revision = view(env)["revision"]
    original_run = changes._run
    external_succeeded = []

    def concurrent_index_writer(root, *args, **kwargs):
        if args and args[0] == "add":
            # An external Git writer either wins and must be preserved, or is
            # excluded by our real index.lock. Neither result permits rollback
            # code to replace an index this operation has not changed.
            external_env = {key: value for key, value in os.environ.items() if key != "GIT_INDEX_FILE"}
            external = subprocess.run(
                ["git", "-C", str(root), "add", "b.txt"],
                capture_output=True, check=False, env=external_env,
            )
            external_succeeded.append(external.returncode == 0)
            if external.returncode:
                assert b"index.lock" in external.stderr
            raise changes.DiffError("another Git process held the index lock")
        return original_run(root, *args, **kwargs)

    monkeypatch.setattr(changes, "_run", concurrent_index_writer)
    response = action(env, "stage", paths=["a.txt"], expected_revision=revision)
    assert response.status_code in {400, 409}, response.text
    assert external_succeeded
    assert git(env.root, "show", ":b.txt") == (b"external staged edit\n" if external_succeeded[0] else b"unrelated\n")
    assert git(env.root, "show", ":a.txt") == b"base\n"
    assert (env.root / "a.txt").read_bytes() == b"requested stage\n"
    assert (env.root / "b.txt").read_bytes() == b"external staged edit\n"


def test_discard_detecting_an_external_edit_never_restores_old_backup_over_it(env, monkeypatch):
    from wright.application.workspace import changes

    (env.root / "a.txt").write_bytes(b"original reviewed change\n")
    revision = view(env)["revision"]
    original_transaction = changes._index_transaction

    @contextmanager
    def external_write_before_revalidation(root, **kwargs):
        with original_transaction(root, **kwargs) as run:
            (root / "a.txt").write_bytes(b"new external user edit\n")
            yield run

    monkeypatch.setattr(changes, "_index_transaction", external_write_before_revalidation)
    response = action(env, "discard", paths=["a.txt"], expected_revision=revision, confirm=True)
    assert response.status_code == 409, response.text
    assert (env.root / "a.txt").read_bytes() == b"new external user edit\n"
    assert git(env.root, "show", ":a.txt") == b"base\n"


def test_missing_execution_root_is_an_error_instead_of_a_clean_workspace(env):
    saved = env.root.with_name("temporarily-moved-worktree")
    env.root.rename(saved)
    try:
        response = env.client.get(f"{env.base}/changes")
        assert response.status_code in {400, 404, 409}, response.text
    finally:
        saved.rename(env.root)


def test_session_undo_keeps_executable_mode_and_git_index(env):
    target = env.root / "a.txt"
    target.chmod(0o755)
    git(env.root, "add", "a.txt")
    git(env.root, "commit", "-m", "executable source")
    env.journal.ensure_baseline()
    before = facts(env.root)
    note(env, "a.txt", "task result\n")
    succeeded(undo(env, ["a.txt"], review(env)["revision"]))
    assert facts(env.root) == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_session_undo_rejects_a_later_symlink_even_if_target_content_matches(env):
    env.journal.ensure_baseline()
    note(env, "a.txt", "task result\n")
    target = env.root / "a.txt"
    outside = env.root.parent / "external.txt"
    outside.write_text("task result\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)
    current = review(env)
    row = next(item for item in current["changes"] if item["path"] == "a.txt")
    assert row["can_revert"] is False
    response = undo(env, ["a.txt"], current["revision"])
    assert response.status_code >= 400 or response.json().get("applied") is False
    assert target.is_symlink()
    assert outside.read_bytes() == b"task result\n"


def test_session_undo_does_not_erase_intervening_user_edit_after_a_later_task_write(env):
    env.journal.ensure_baseline()
    note(env, "a.txt", "first task edit\n")
    (env.root / "a.txt").write_text("intervening user edit\n", encoding="utf-8")
    note(env, "a.txt", "task integrates user edit\n")
    before = facts(env.root)
    current = review(env)
    row = next(item for item in current["changes"] if item["path"] == "a.txt")
    assert row["can_revert"] is False
    response = undo(env, ["a.txt"], current["revision"])
    assert response.status_code >= 400 or response.json().get("applied") is False
    assert facts(env.root) == before


def test_session_staged_rename_undo_restores_pair_but_preserves_current_index(env):
    (env.root / "b.txt").write_text("unrelated user edit\n", encoding="utf-8")
    before = env.journal.capture()
    git(env.root, "mv", "a.txt", "renamed.txt")
    env.journal.commit_capture(before, call_id="rename-call", tool_name="execute_command")
    current = review(env)
    task_paths = {row["path"] for row in current["changes"] if row.get("origin") == "task"}
    assert task_paths == {"a.txt", "renamed.txt"}
    before_undo = facts(env.root)
    succeeded(undo(env, ["a.txt", "renamed.txt"], current["revision"]))
    after = facts(env.root)
    assert after["files"] == {"a.txt": b"base\n", "b.txt": b"unrelated user edit\n"}
    assert after["index"] == before_undo["index"]
    assert after["staged"] == before_undo["staged"]
    assert after["head"] == before_undo["head"]


def test_worktree_stage_and_commit_never_retarget_the_primary_checkout(env):
    primary = env.root
    (primary / "a.txt").write_bytes(b"primary user work\n")
    primary_before = facts(primary)
    worktree = primary.parent / "isolated-worktree"
    base = git(primary, "rev-parse", "HEAD").decode().strip()
    git(primary, "worktree", "add", "-b", "acceptance-isolation", str(worktree), "HEAD")
    env.root = worktree
    env.state.workspace_dir = worktree
    env.runtime.project_context = ProjectContext(
        project_root=primary, execution_root=worktree, environment="worktree",
        base_commit=base, branch_name="acceptance-isolation",
    )
    (worktree / "a.txt").write_bytes(b"isolated task work\n")
    assert view(env)["environment"] == "worktree"
    assert "+isolated task work" in patch(env)["patch"]
    succeeded(action(env, "stage", scope="unstaged", paths=["a.txt"]))
    succeeded(action(env, "commit", scope="staged", confirm=True, message="isolated change"))
    assert view(env)["changes"] == []
    assert git(worktree, "show", "HEAD:a.txt") == b"isolated task work\n"
    assert facts(primary) == primary_before


def test_staged_rename_unstage_restage_and_discard_preserve_unrelated_work(env):
    (env.root / "b.txt").write_bytes(b"unrelated user edit\n")
    git(env.root, "mv", "a.txt", "renamed.txt")
    head_before = git(env.root, "rev-parse", "HEAD")
    succeeded(action(env, "unstage", scope="staged", paths=["renamed.txt"]))
    assert not (env.root / "a.txt").exists()
    assert (env.root / "renamed.txt").read_bytes() == b"base\n"
    assert git(env.root, "show", ":a.txt") == b"base\n"
    succeeded(action(env, "stage", scope="working", paths=["a.txt", "renamed.txt"]))
    rename = next(row for row in view(env, "staged")["changes"] if row["path"] == "renamed.txt")
    assert rename["old_path"] == "a.txt"
    succeeded(action(env, "discard", scope="working", paths=["renamed.txt"], confirm=True))
    assert (env.root / "a.txt").read_bytes() == b"base\n"
    assert not (env.root / "renamed.txt").exists()
    assert (env.root / "b.txt").read_bytes() == b"unrelated user edit\n"
    assert not git(env.root, "diff", "--cached", "--name-only")
    assert git(env.root, "rev-parse", "HEAD") == head_before


def test_failed_batch_discard_restores_every_original_file_and_index(env, monkeypatch):
    from wright.application.workspace import changes

    (env.root / "a.txt").write_bytes(b"user first\n")
    (env.root / "b.txt").write_bytes(b"user second\n")
    git(env.root, "add", "a.txt")
    before = facts(env.root)
    original_write = changes._write_atomic

    def fail_second_restore(path, data, mode=None):
        if path == env.root / "b.txt" and data == b"unrelated\n":
            raise OSError("simulated second path write failure")
        return original_write(path, data, mode)

    monkeypatch.setattr(changes, "_write_atomic", fail_second_restore)
    response = action(env, "discard", paths=["a.txt", "b.txt"], confirm=True)
    assert response.status_code in {400, 409}, response.text
    assert facts(env.root) == before


def test_batch_discard_rollback_preserves_newer_external_bytes_on_already_written_path(env, monkeypatch):
    from wright.application.workspace import changes

    (env.root / "a.txt").write_bytes(b"user first\n")
    (env.root / "b.txt").write_bytes(b"user second\n")
    git(env.root, "add", "a.txt")
    before = facts(env.root)
    original_write = changes._write_atomic

    def external_edit_during_second_restore(path, data, mode=None):
        if path == env.root / "b.txt" and data == b"unrelated\n":
            assert (env.root / "a.txt").read_bytes() == b"base\n"
            (env.root / "a.txt").write_bytes(b"newer external user bytes\n")
            raise OSError("simulated second path write failure")
        return original_write(path, data, mode)

    monkeypatch.setattr(changes, "_write_atomic", external_edit_during_second_restore)
    response = action(env, "discard", paths=["a.txt", "b.txt"], confirm=True)
    assert response.status_code in {400, 409}, response.text
    after = facts(env.root)
    assert after["files"] == {"a.txt": b"newer external user bytes\n", "b.txt": b"user second\n"}
    assert after["head"] == before["head"]
    assert after["index"] == before["index"]
    assert after["staged"] == before["staged"]


@pytest.mark.parametrize("kind", ["hardlink", "symlink-parent"])
def test_unsafe_alias_paths_never_change_the_linked_user_data(env, kind):
    target = env.root / "a.txt"
    if kind == "hardlink":
        outside = env.root.parent / "external-hardlink.txt"
        os.link(target, outside)
        target.write_bytes(b"shared user bytes\n")
        selected = "a.txt"
    else:
        directory = env.root / "nested"
        directory.mkdir()
        (directory / "file.txt").write_bytes(b"tracked nested base\n")
        git(env.root, "add", "nested/file.txt")
        git(env.root, "commit", "-m", "nested baseline")
        (directory / "file.txt").unlink()
        directory.rmdir()
        outside = env.root.parent / "external-directory"
        outside.mkdir()
        (outside / "file.txt").write_bytes(b"external user bytes\n")
        directory.symlink_to(outside, target_is_directory=True)
        selected = "nested/file.txt"
    before = facts(env.root)
    external_before = outside.read_bytes() if kind == "hardlink" else (outside / "file.txt").read_bytes()
    response = action(env, "discard", paths=[selected], confirm=True)
    assert response.status_code in {400, 409}, response.text
    assert facts(env.root) == before
    external_after = outside.read_bytes() if kind == "hardlink" else (outside / "file.txt").read_bytes()
    assert external_after == external_before


def subdirectory_workspace(env):
    repository = env.root
    workspace = repository / "sub"
    workspace.mkdir()
    (workspace / "inside.txt").write_bytes(b"inside base\n")
    git(repository, "add", "sub/inside.txt")
    git(repository, "commit", "-m", "subdirectory baseline")
    env.root = workspace
    env.state.project_root = workspace
    env.state.workspace_dir = workspace
    env.runtime.project_context = ProjectContext.local(workspace)
    (workspace / "inside.txt").write_bytes(b"inside staged edit\n")
    git(repository, "add", "sub/inside.txt")
    (repository / "a.txt").write_bytes(b"outside user edit\n")
    return repository


@pytest.mark.parametrize("outside_staged", [True, False])
def test_subdirectory_commit_only_succeeds_when_all_staged_paths_are_visible(env, outside_staged):
    repository = subdirectory_workspace(env)
    if outside_staged:
        git(repository, "add", "a.txt")
    staged = view(env, "staged")
    assert [row["path"] for row in staged["changes"]] == ["inside.txt"]
    before = facts(repository)
    response = action(
        env, "commit", scope="staged", expected_revision=staged["revision"],
        confirm=True, message="commit visible subdirectory",
    )
    if outside_staged:
        assert response.status_code in {400, 409}, response.text
        assert facts(repository) == before
    else:
        succeeded(response)
        after = facts(repository)
        assert after["head"] != before["head"]
        assert after["index"] == before["index"]
        assert after["files"] == before["files"]
        assert git(repository, "show", "--format=", "--name-only", "HEAD").strip() == b"sub/inside.txt"
        assert git(repository, "show", "HEAD:a.txt") == b"base\n"
        assert (repository / "a.txt").read_bytes() == b"outside user edit\n"


def test_subdirectory_commit_rechecks_hidden_staging_after_transaction_begins(env, monkeypatch):
    from wright.application.workspace import changes

    repository = subdirectory_workspace(env)
    revision = view(env, "staged")["revision"]
    original_transaction = changes._index_transaction
    externally_staged = []

    @contextmanager
    def external_stage_before_index_reservation(root, **kwargs):
        # The first commit-scope check has passed. An external Git process
        # stages an out-of-workspace file before our index reservation.
        git(repository, "add", "a.txt")
        externally_staged.append(facts(repository))
        with original_transaction(root, **kwargs) as run:
            yield run

    monkeypatch.setattr(changes, "_index_transaction", external_stage_before_index_reservation)
    response = action(
        env, "commit", scope="staged", expected_revision=revision,
        confirm=True, message="must not commit hidden late staging",
    )
    assert response.status_code in {400, 409}, response.text
    assert len(externally_staged) == 1
    assert facts(repository) == externally_staged[0]
    assert git(repository, "show", ":a.txt") == b"outside user edit\n"
