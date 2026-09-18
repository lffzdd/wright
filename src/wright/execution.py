"""Local execution boundary. Permission is decided before this backend runs."""

from __future__ import annotations

from pathlib import Path


class LocalExecutionBackend:
    """Own path containment for the existing local/worktree execution root.

    This is an architectural boundary, not a sandbox: commands still run on
    the host and worktrees only isolate the checkout contents.
    """

    def __init__(self, workspace_dir: Path, cwd_provider) -> None:
        self.workspace_dir = workspace_dir.resolve()
        self._cwd_provider = cwd_provider

    def cwd(self) -> Path:
        return self._cwd_provider().resolve()

    def path(self, requested: str) -> Path:
        candidate = Path(requested)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.cwd() / candidate).resolve()
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError as exc:
            # Keep the long-standing user-visible file-tool diagnostic while
            # centralising the containment decision in the execution backend.
            raise ValueError("Unsafe path") from exc
        return resolved
