"""Logical project and execution-directory identity.

The project root owns durable Wright state.  The execution root is where an
agent actually reads and writes files and may be an isolated git worktree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ExecutionEnvironment = Literal["local", "worktree"]


@dataclass(frozen=True)
class ProjectContext:
    project_root: Path
    execution_root: Path
    environment: ExecutionEnvironment = "local"
    base_commit: str | None = None
    branch_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", self.project_root.expanduser().resolve())
        object.__setattr__(
            self,
            "execution_root",
            self.execution_root.expanduser().resolve(),
        )
        if self.environment not in {"local", "worktree"}:
            raise ValueError(f"unknown execution environment: {self.environment}")
        if self.environment == "worktree" and not self.base_commit:
            raise ValueError("worktree environments require base_commit")

    @classmethod
    def local(cls, root: Path) -> ProjectContext:
        resolved = root.expanduser().resolve()
        return cls(project_root=resolved, execution_root=resolved)
