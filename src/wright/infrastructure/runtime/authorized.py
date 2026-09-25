"""Per-invocation capability wrapper around an execution backend."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

from ...domain.policy.types import GrantTarget, InvocationGrant, PermissionOperation
from .protocols import ExecutionBackend, ProcessHandle
from .types import DirectoryEntry, ExecutionPath, FileMetadata, SearchMatch


class AuthorizedExecution:
    """Enforce an immutable :class:`InvocationGrant` on every backend call."""

    def __init__(
        self,
        backend: ExecutionBackend,
        grant: InvocationGrant,
        *,
        dynamic_cwd: bool = False,
    ):
        if backend.environment_id != grant.environment_id:
            raise ValueError("grant and execution backend use different environments")
        self._backend = backend
        self.grant = grant
        # Only direct tool-test adapters use a live cwd. Production callers
        # leave this false so the grant remains bound to the resolver's fixed
        # cwd for the whole invocation.
        self._dynamic_cwd = dynamic_cwd
        self._closed = False

    @property
    def environment_id(self) -> str:
        return self.grant.environment_id

    def close(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise PermissionError("authorized execution is closed")

    def _operation(self, operation: PermissionOperation) -> None:
        self._ensure_open()
        if operation not in self.grant.operations:
            raise PermissionError(f"invocation is not authorized for {operation}")

    def _target(self, path: ExecutionPath, operation: PermissionOperation) -> ExecutionPath:
        self._operation(operation)
        if path.environment_id != self.environment_id:
            raise PermissionError("path belongs to another execution environment")
        current = self._backend.revalidate_path(path)
        self._ensure_not_blocked(current)
        if not any(
            item.operation == operation and _matches(current, item)
            for item in self.grant.targets
        ):
            raise PermissionError("path is outside this invocation grant")
        return current

    def cwd(self) -> ExecutionPath:
        self._ensure_open()
        if self._dynamic_cwd:
            return self._backend.cwd()
        if self.grant.cwd.environment_id != self.environment_id:
            raise PermissionError("cwd belongs to another execution environment")
        return self._backend.revalidate_path(self.grant.cwd)

    def resolve_path(self, requested: str) -> ExecutionPath:
        self._ensure_open()
        path = self._backend.resolve_path(requested, cwd=self.cwd())
        for operation in ("file_read", "file_write"):
            try:
                return self._target(path, operation)
            except PermissionError:
                continue
        raise PermissionError("path is outside this invocation grant")

    def display_path(self, path: ExecutionPath) -> str:
        self._ensure_open()
        current = self._backend.revalidate_path(path)
        self._ensure_not_blocked(current)
        return self._backend.display_path(current)

    def metadata(self, path: ExecutionPath) -> FileMetadata:
        return self._backend.metadata(self._target(path, "file_read"))

    def read_bytes(self, path: ExecutionPath, limit: int | None = None) -> bytes:
        return self._backend.read_bytes(self._target(path, "file_read"), limit)

    def read_text(self, path: ExecutionPath, *, encoding: str, errors: str = "strict") -> str:
        return self._backend.read_text(
            self._target(path, "file_read"), encoding=encoding, errors=errors
        )

    def write_text(self, path: ExecutionPath, content: str, *, encoding: str) -> None:
        self._backend.write_text(self._target(path, "file_write"), content, encoding=encoding)

    def ensure_directory(self, path: ExecutionPath) -> None:
        """Create only a parent explicitly covered by a file-write grant."""

        self._operation("file_write")
        current = self._backend.revalidate_path(path)
        self._ensure_not_blocked(current)
        if not any(
            item.operation == "file_write"
            and (
                (item.recursive and _is_under(current, item.path))
                or current == item.path.parent
            )
            for item in self.grant.targets
        ):
            raise PermissionError("directory is outside this invocation grant")
        self._backend.ensure_directory(current)

    def iter_directory(self, path: ExecutionPath) -> Iterable[DirectoryEntry]:
        current = self._target(path, "file_read")
        for entry in self._backend.iter_directory(current):
            # Filter before the caller observes metadata/content.  The local
            # backend itself also avoids following symlinked directories.
            try:
                yield DirectoryEntry(
                    self._target(entry.path, "file_read"), entry.metadata
                )
            except PermissionError:
                continue

    def glob(self, path: ExecutionPath, pattern: str) -> Iterable[DirectoryEntry]:
        current = self._target(path, "file_read")
        for entry in self._backend.glob(current, pattern):
            try:
                yield DirectoryEntry(
                    self._target(entry.path, "file_read"), entry.metadata
                )
            except PermissionError:
                continue

    def iter_search_candidates(
        self,
        path: ExecutionPath,
        *,
        glob: str | None = None,
        deadline: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Iterable[ExecutionPath]:
        current = self._target(path, "file_read")
        metadata = self._backend.metadata(current)
        if metadata.kind not in {"file", "directory"}:
            return
        candidates = self._backend.iter_search_candidates(
            current,
            glob=glob,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        try:
            for candidate in candidates:
                try:
                    authorized = self._target(candidate, "file_read")
                    if self._backend.metadata(authorized).kind == "file":
                        yield authorized
                except PermissionError:
                    continue
        finally:
            close = getattr(candidates, "close", None)
            if callable(close):
                close()

    def search_files(
        self,
        paths: Iterable[ExecutionPath],
        pattern: str,
        *,
        case_sensitive: bool = False,
        fixed_string: bool = False,
        deadline: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Iterable[SearchMatch]:
        """Pass only revalidated candidates to the backend content search."""

        def authorized_candidates() -> Iterable[ExecutionPath]:
            for path in paths:
                try:
                    yield self._target(path, "file_read")
                except PermissionError:
                    continue

        candidates = authorized_candidates()
        matches = self._backend.search_files(
            candidates,
            pattern,
            case_sensitive=case_sensitive,
            fixed_string=fixed_string,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        try:
            for match in matches:
                try:
                    authorized = self._target(match.path, "file_read")
                except PermissionError:
                    continue
                yield SearchMatch(authorized, match.line, match.column, match.text)
        finally:
            close = getattr(matches, "close", None)
            if callable(close):
                close()
            close = getattr(candidates, "close", None)
            if callable(close):
                close()

    def start_shell(self, command: str) -> ProcessHandle:
        self._operation("shell")
        if self.grant.command is not None and (
            self.grant.subject != command or self.grant.command != command
        ):
            raise PermissionError("shell command differs from the approved command")
        return self._backend.start_shell(command, cwd=self.cwd())

    def _ensure_not_blocked(self, path: ExecutionPath) -> None:
        if any(_is_under(path, blocked) for blocked in self.grant.blocked_paths):
            raise PermissionError("path is protected from this invocation")


def _is_under(path: ExecutionPath, root: ExecutionPath) -> bool:
    try:
        return os.path.commonpath((path.value, root.value)) == root.value
    except ValueError:
        return False


def _matches(path: ExecutionPath, target: GrantTarget) -> bool:
    return path == target.path or (target.recursive and _is_under(path, target.path))
