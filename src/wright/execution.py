"""Local execution boundary. Permission is decided before this backend runs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from threading import Lock
from typing import Any

from .access import AccessScope, PathClass, is_under, resolve_path


class LocalExecutionBackend:
    """Own path containment for authorized working directories.

    This is an architectural boundary, not a sandbox: commands still run on
    the host and worktrees only isolate the checkout contents. After permission
    allows a call, this backend accepts origin roots, extra granted roots, and
    one-shot invocation paths. Forbidden paths stay blocked.
    """

    def __init__(
        self,
        workspace_dir: Path,
        cwd_provider,
        additional: tuple[Path, ...] | list[Path] = (),
    ) -> None:
        self.workspace_dir = workspace_dir.resolve()
        self._cwd_provider = cwd_provider
        self.access = AccessScope(self.workspace_dir, additional)
        self._invocation_lock = Lock()
        self._invocation_paths: tuple[Path, ...] = ()

    def cwd(self) -> Path:
        return self._cwd_provider().resolve()

    def resolve_path(self, requested: str) -> Path:
        candidate = Path(requested).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.cwd() / candidate).resolve()

    def set_invocation_paths(self, paths: list[Path] | tuple[Path, ...]) -> None:
        resolved = tuple(resolve_path(path) for path in paths)
        with self._invocation_lock:
            self._invocation_paths = resolved

    def clear_invocation_paths(self) -> None:
        with self._invocation_lock:
            self._invocation_paths = ()

    def invocation_allows(self, path: Path) -> bool:
        resolved = resolve_path(path)
        with self._invocation_lock:
            allowed = self._invocation_paths
        return any(
            resolved == item or is_under(resolved, item) for item in allowed
        )

    def classify_path(self, requested: str) -> tuple[Path, PathClass]:
        resolved = self.resolve_path(requested)
        return resolved, self.access.classify(resolved)

    def display_path(self, path: Path) -> str:
        resolved = resolve_path(path)
        try:
            relative = resolved.relative_to(self.workspace_dir)
        except ValueError:
            return str(resolved)
        text = str(relative)
        return text if text != "." else "."

    def path(self, requested: str) -> Path:
        resolved, classification = self.classify_path(requested)
        if classification == PathClass.FORBIDDEN:
            raise ValueError("Unsafe path")
        if classification in {PathClass.IN_ORIGIN, PathClass.IN_GRANTED}:
            return resolved
        if self.invocation_allows(resolved):
            return resolved
        raise ValueError("Unsafe path")

    def sync_additional(self, directories: tuple[Path, ...] | list[Path]) -> None:
        self.access.sync_additional(directories)

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
