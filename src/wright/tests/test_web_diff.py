import subprocess

import pytest

from ..project import ProjectContext
from ..web.diff import DiffError, change_patch, list_changes


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "wright@example.test")
    _git(root, "config", "user.name", "Wright Tests")
    (root / "tracked.txt").write_text("base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "base")
    return root


def test_local_diff_lists_tracked_and_untracked_and_returns_patch(tmp_path):
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("changed\n")
    (root / "new.txt").write_text("new\n")
    context = ProjectContext.local(root)

    summary = list_changes(context)
    paths = {item["path"] for item in summary["changes"]}

    assert paths == {"tracked.txt", "new.txt"}
    assert summary["local_warning"] is True
    assert "+changed" in change_patch(context, "tracked.txt")["patch"]
    assert "+new" in change_patch(context, "new.txt")["patch"]


def test_diff_rejects_traversal_and_escaping_symlink(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "escape.txt").symlink_to(outside)

    with pytest.raises(DiffError, match="invalid"):
        change_patch(ProjectContext.local(root), "../outside.txt")
    with pytest.raises(DiffError, match="escapes"):
        change_patch(ProjectContext.local(root), "escape.txt")


def test_large_and_binary_changes_return_metadata_only(tmp_path):
    root = _repo(tmp_path)
    (root / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    large = change_patch(ProjectContext.local(root), "large.bin")
    (root / "binary.bin").write_bytes(b"a\0b")
    binary = change_patch(ProjectContext.local(root), "binary.bin")

    assert large["truncated"] is True and large["patch"] == ""
    assert binary["binary"] is True and binary["patch"] == ""
