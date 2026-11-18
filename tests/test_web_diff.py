import subprocess

import pytest

from wright.domain.model.workspace.project import ProjectContext
from wright.interfaces.web.diff import DiffError, change_patch, list_changes


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


@pytest.mark.parametrize("tracked", [True, False])
def test_binary_marker_words_in_source_are_normal_text_changes(tmp_path, tracked):
    root = _repo(tmp_path)
    path = "tracked.txt" if tracked else "new.py"
    (root / path).write_text(
        'binary_markers = ["Binary files ", "GIT binary patch"]\n',
        encoding="utf-8",
    )
    result = change_patch(ProjectContext.local(root), path)
    assert result["binary"] is False
    assert result["truncated"] is False
    assert result["additions"] == 1
    assert '+binary_markers = ["Binary files ", "GIT binary patch"]' in result["patch"]


def _tracked_changes(tmp_path, count):
    tmp_path.mkdir()
    root = _repo(tmp_path)
    for index in range(count):
        (root / f"file {index}.txt").write_text("base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "more")
    for index in range(count):
        (root / f"file {index}.txt").write_text(f"base\nline {index}\n")
    return root


def _count_git_calls(monkeypatch, root, scope):
    from wright.application.workspace import changes

    calls = []
    original = changes._run

    def counting(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(changes, "_run", counting)
    list_changes(ProjectContext.local(root), scope)
    monkeypatch.setattr(changes, "_run", original)
    return len(calls)


@pytest.mark.parametrize("scope", ["working", "unstaged", "staged"])
def test_listing_git_calls_do_not_grow_with_tracked_file_count(tmp_path, monkeypatch, scope):
    few = _tracked_changes(tmp_path / "few", 2)
    many = _tracked_changes(tmp_path / "many", 12)
    if scope == "staged":
        _git(few, "add", ".")
        _git(many, "add", ".")

    assert _count_git_calls(monkeypatch, many, scope) == _count_git_calls(monkeypatch, few, scope)


def test_batched_listing_keeps_per_file_stats_for_tricky_paths(tmp_path):
    root = _repo(tmp_path)
    names = ["space name.txt", "naïve.txt", 'quote".txt', "mode.sh", "old.txt", "gone.txt"]
    for name in names:
        (root / name).write_text("one\ntwo\nthree\nfour\nfive\n")
    (root / "old.txt").write_text("moved 1\nmoved 2\nmoved 3\nmoved 4\nmoved 5\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "tricky")
    (root / "space name.txt").write_text("one\ntwo\n")
    (root / "naïve.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
    (root / 'quote".txt').write_text("ONE\ntwo\nthree\nfour\nfive\n")
    (root / "mode.sh").chmod(0o755)
    _git(root, "mv", "old.txt", "new.txt")
    (root / "new.txt").write_text("moved 1\nmoved 2\nmoved 3\nmoved 4\nmoved 5\nmoved 6\n")
    (root / "gone.txt").unlink()
    (root / "fresh.txt").write_text("a\nb\n")

    rows = {row["path"]: row for row in list_changes(ProjectContext.local(root))["changes"]}

    stats = {path: (row["additions"], row["deletions"]) for path, row in rows.items()}
    assert stats == {
        "space name.txt": (0, 3),
        "naïve.txt": (1, 0),
        'quote".txt': (1, 1),
        "mode.sh": (0, 0),
        "new.txt": (1, 0),
        "gone.txt": (0, 5),
        "fresh.txt": (2, 0),
    }
    assert rows["new.txt"]["old_path"] == "old.txt"
    assert rows["space name.txt"]["capabilities"]["hunk"] is False  # working scope never offers hunks
    for path in ("space name.txt", "naïve.txt", 'quote".txt', "new.txt"):
        assert change_patch(ProjectContext.local(root), path)["additions"] == stats[path][0]
