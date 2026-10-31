"""Worktree failures and the outcome of archiving one."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveResult:
    removed: bool
    retained: bool
    reason: str
    path: Path
    branch_name: str | None = None
