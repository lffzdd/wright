"""Paths the native sandboxes keep read-only inside writable roots."""

from types import SimpleNamespace

from wright.domain.gateway.execution import ExecutionPath
from wright.infrastructure.runtime.sandbox_profile import git_control_paths


def _gitdir(path):
    (path / "hooks").mkdir(parents=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n")
    (path / "config").write_text("[core]\n")
    return path


def _grant(root):
    path = ExecutionPath("local", str(root))
    return SimpleNamespace(shell_readable=(path,), shell_writable=(path,), targets=())


def test_git_control_paths_cover_submodule_gitdirs(tmp_path):
    main = _gitdir(tmp_path / ".git")
    plain = _gitdir(main / "modules" / "sub")
    slashed = _gitdir(main / "modules" / "lib" / "sub")
    nested = _gitdir(plain / "modules" / "inner")

    paths = set(git_control_paths(_grant(tmp_path)))

    for gitdir in (main, plain, slashed, nested):
        assert {gitdir / "config", gitdir / "config.worktree", gitdir / "hooks"} <= paths, gitdir


def test_git_control_paths_ignore_directories_that_are_not_gitdirs(tmp_path):
    main = _gitdir(tmp_path / ".git")
    (main / "modules" / "partial").mkdir(parents=True)

    assert not any("partial" in str(path) for path in git_control_paths(_grant(tmp_path)))
