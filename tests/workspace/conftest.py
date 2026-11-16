"""Session-scoped Git seeds so each Changes test clones a finished repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _seed(root: Path, files: dict[str, bytes]) -> Path:
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "Tests")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "gc.auto", "0")
    _git(root, "config", "maintenance.auto", "false")
    for name, content in files.items():
        (root / name).write_bytes(content)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _clone(seed: Path, dest: Path) -> Path:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    subprocess.run(
        [
            "git", "clone", "--quiet", "--local", "--no-hardlinks",
            "-c", "user.email=tests@example.test",
            "-c", "user.name=Tests",
            "-c", "commit.gpgsign=false",
            "-c", "gc.auto=0",
            str(seed), str(dest),
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    return dest


@pytest.fixture(scope="session")
def git_seed_other(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _seed(
        tmp_path_factory.mktemp("git-seed-other"),
        {"a.txt": b"base\n", "b.txt": b"other\n"},
    )


@pytest.fixture(scope="session")
def git_seed_unrelated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _seed(
        tmp_path_factory.mktemp("git-seed-unrelated"),
        {"a.txt": b"base\n", "b.txt": b"unrelated\n"},
    )


@pytest.fixture
def git_workspace_other(tmp_path: Path, git_seed_other: Path) -> Path:
    return _clone(git_seed_other, tmp_path / "repo")


@pytest.fixture
def git_workspace_unrelated(tmp_path: Path, git_seed_unrelated: Path) -> Path:
    return _clone(git_seed_unrelated, tmp_path / "repo")
