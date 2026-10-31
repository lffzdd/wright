import subprocess

from wright.application.workspace.git_status import summarize_git


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "qa@example.test")
    git(root, "config", "user.name", "QA")
    (root / "tracked.txt").write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def test_summary_counts_individual_new_files_and_a_rename_once(tmp_path):
    root = repo(tmp_path)
    git(root, "mv", "tracked.txt", "renamed.txt")
    folder = root / "new"
    folder.mkdir()
    (folder / "one.txt").write_text("one\n")
    (folder / "two\nlines.txt").write_text("two\n")
    assert summarize_git(root)["uncommitted_count"] == 3


def test_summary_of_subdirectory_excludes_other_workspace_changes(tmp_path):
    root = repo(tmp_path)
    (root / "tracked.txt").write_text("outside edit\n")
    folder = root / "sub"
    folder.mkdir()
    (folder / "inside.txt").write_text("inside edit\n")
    assert summarize_git(folder)["uncommitted_count"] == 1
