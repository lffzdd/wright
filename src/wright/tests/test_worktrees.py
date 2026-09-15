import os
import subprocess

from ..project import ProjectContext
from ..worktrees import WorktreeManager


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "wright@example.test")
    _git(root, "config", "user.name", "Wright Tests")
    (root / "tracked.txt").write_text("base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "base")
    return root


def test_worktree_starts_at_head_without_dirty_checkout(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("dirty checkout\n")
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "wright-home"))
    manager = WorktreeManager(root)

    context = manager.create("abc123")

    assert context.base_commit == _git(root, "rev-parse", "HEAD")
    assert context.branch_name == "wright/abc123"
    assert (context.execution_root / "tracked.txt").read_text() == "base\n"
    assert context.project_root == root.resolve()
    result = manager.archive(context)
    assert result.removed is True
    assert not context.execution_root.exists()


def test_dirty_worktree_archive_is_retained(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "wright-home"))
    manager = WorktreeManager(root)
    context = manager.create("dirty123")
    (context.execution_root / "new.txt").write_text("keep me\n")

    result = manager.archive(context)

    assert result.retained is True
    assert context.execution_root.is_dir()
    assert "uncommitted" in result.reason


def test_non_git_project_falls_back_to_local(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()

    context = WorktreeManager(root).create("plain123")

    assert context == ProjectContext.local(root)


def test_new_commit_worktree_archive_is_retained(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "wright-home"))
    manager = WorktreeManager(root)
    context = manager.create("commit123")
    (context.execution_root / "tracked.txt").write_text("committed\n")
    _git(context.execution_root, "add", "tracked.txt")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Wright", "GIT_AUTHOR_EMAIL": "wright@example.test", "GIT_COMMITTER_NAME": "Wright", "GIT_COMMITTER_EMAIL": "wright@example.test"}
    subprocess.run(["git", "-C", str(context.execution_root), "commit", "-m", "change"], check=True, env=env, capture_output=True)

    result = manager.archive(context)

    assert result.retained is True
    assert "new commit" in result.reason
