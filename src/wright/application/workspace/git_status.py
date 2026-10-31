"""Read-only git facts for one execution root.

The process cwd is not changed. A directory that is not a git checkout
reports that fact instead of a fabricated branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...core.git import git_command


def summarize_git(root: Path) -> dict[str, object]:
    resolved = root.expanduser().resolve()
    if not (resolved / ".git").exists() and not _inside_git(resolved):
        return {
            "git": False,
            "branch": None,
            "uncommitted_count": 0,
            "dirty": False,
        }
    branch = _git(resolved, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    status = _git(resolved, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    prefix = _git(resolved, "rev-parse", "--show-prefix").strip()
    entries = iter(status.split("\0"))
    count = 0
    for entry in entries:
        if not entry:
            continue
        if entry[0] in {"R", "C"}:
            next(entries, None)
        if not prefix or entry[3:].startswith(prefix):
            count += 1
    return {
        "git": True,
        "branch": branch,
        "uncommitted_count": count,
        "dirty": bool(count),
    }


def _inside_git(root: Path) -> bool:
    result = subprocess.run(
        git_command(root, "rev-parse", "--is-inside-work-tree"),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        git_command(root, *args),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


__all__ = ["summarize_git"]
