"""Safe git worktree lifecycle for parallel Web sessions."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import project_id, wright_home
from .project import ProjectContext


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveResult:
    removed: bool
    retained: bool
    reason: str
    path: Path
    branch_name: str | None = None


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


class WorktreeManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.expanduser().resolve()

    @property
    def is_git(self) -> bool:
        return bool(_git(self.project_root, "rev-parse", "--is-inside-work-tree", check=False))

    @property
    def current_head(self) -> str:
        if not self.is_git:
            raise WorktreeError("workspace is not a git repository")
        return _git(self.project_root, "rev-parse", "HEAD")

    def create(self, session_id: str) -> ProjectContext:
        if not self.is_git:
            return ProjectContext.local(self.project_root)
        base_commit = self.current_head
        branch_name = f"wright/{session_id}"
        destination = (
            wright_home()
            / "worktrees"
            / project_id(self.project_root)
            / session_id
        ).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise WorktreeError(f"worktree path already exists: {destination}")
        _git(
            self.project_root,
            "worktree",
            "add",
            "-b",
            branch_name,
            str(destination),
            base_commit,
        )
        return ProjectContext(
            project_root=self.project_root,
            execution_root=destination,
            environment="worktree",
            base_commit=base_commit,
            branch_name=branch_name,
        )

    def _validate_worktree_context(self, context: ProjectContext) -> None:
        expected_root = (
            wright_home() / "worktrees" / project_id(self.project_root)
        ).resolve()
        try:
            context.execution_root.resolve().relative_to(expected_root)
        except ValueError as exc:
            raise WorktreeError("worktree path is outside Wright's managed root") from exc
        if not context.branch_name or not context.branch_name.startswith("wright/"):
            raise WorktreeError("worktree branch is not managed by Wright")

    def inspect(self, context: ProjectContext) -> dict[str, object]:
        if context.environment != "worktree":
            return {
                "dirty": bool(
                    self.is_git
                    and _git(context.execution_root, "status", "--porcelain", check=False)
                ),
                "new_commits": 0,
            }
        self._validate_worktree_context(context)
        dirty = bool(_git(context.execution_root, "status", "--porcelain"))
        new_commits = int(
            _git(
                context.execution_root,
                "rev-list",
                "--count",
                f"{context.base_commit}..HEAD",
            )
        )
        return {"dirty": dirty, "new_commits": new_commits}

    def archive(self, context: ProjectContext) -> ArchiveResult:
        if context.environment != "worktree":
            return ArchiveResult(
                removed=False,
                retained=True,
                reason="local checkout is retained",
                path=context.execution_root,
            )
        self._validate_worktree_context(context)
        state = self.inspect(context)
        if state["dirty"] or state["new_commits"]:
            details = []
            if state["dirty"]:
                details.append("uncommitted or untracked changes")
            if state["new_commits"]:
                details.append(f"{state['new_commits']} new commit(s)")
            return ArchiveResult(
                removed=False,
                retained=True,
                reason="; ".join(details),
                path=context.execution_root,
                branch_name=context.branch_name,
            )
        _git(self.project_root, "worktree", "remove", str(context.execution_root))
        if context.branch_name:
            _git(self.project_root, "branch", "-D", context.branch_name)
        return ArchiveResult(
            removed=True,
            retained=False,
            reason="clean worktree with no new commits removed",
            path=context.execution_root,
            branch_name=context.branch_name,
        )
