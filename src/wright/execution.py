"""Local execution boundary. Permission is decided before this backend runs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


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

    # These operations intentionally remain small.  File tools keep their
    # validation/edit semantics, while this backend owns the actual local
    # filesystem and process boundary so another backend can replace it.
    def read_bytes(self, path: Path, limit: int | None = None) -> bytes:
        with path.open("rb") as source:
            return source.read() if limit is None else source.read(limit)

    def read_text(self, path: Path, *, encoding: str, errors: str = "strict") -> str:
        return path.read_text(encoding=encoding, errors=errors)

    def write_text(self, path: Path, content: str, *, encoding: str) -> None:
        path.write_text(content, encoding=encoding)

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def stat(self, path: Path):
        return path.stat()

    def iter_directory(self, path: Path):
        return path.iterdir()

    def glob(self, path: Path, pattern: str):
        return path.glob(pattern)

    def ensure_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def start_process(self, argv: list[str], *, cwd: Path, **kwargs: Any) -> subprocess.Popen:
        return subprocess.Popen(argv, cwd=cwd, **kwargs)

    def terminate_process(self, process: subprocess.Popen) -> None:
        """Request process-group termination; RuntimeResources retains its handle."""
        from .processes import terminate_process_tree

        terminate_process_tree(process)

    def iter_process_output(self, process: subprocess.Popen):
        """Yield the merged stdout stream of a backend-owned process."""
        if process.stdout is None:
            return iter(())
        return iter(process.stdout)

    def wait_process(self, process: subprocess.Popen, timeout: float | None = None) -> int:
        return process.wait(timeout=timeout)

    def run_process(self, argv: list[str], *, cwd: Path, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=cwd, **kwargs)
