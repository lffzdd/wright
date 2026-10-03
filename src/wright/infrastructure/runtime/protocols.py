"""Replaceable execution and process contracts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from ...domain.policy.permission.types import InvocationGrant
from .types import (
    DirectoryEntry,
    ExecutionPath,
    FileMetadata,
    SearchMatch,
)


@runtime_checkable
class ProcessHandle(Protocol):
    """A backend-owned process handle; no caller may depend on ``Popen``."""

    def read_output(self, max_bytes: int) -> bytes | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    @property
    def returncode(self) -> int | None: ...

    def group_alive(self) -> bool: ...

    def terminate(self, *, grace_seconds: float = 2.0) -> bool: ...

    def cwd_result(self) -> ExecutionPath | None: ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """Environment operations available to a permission-authorized wrapper."""

    environment_id: str

    def cwd(self) -> ExecutionPath: ...

    def resolve_path(
        self, requested: str, *, cwd: ExecutionPath | None = None
    ) -> ExecutionPath: ...

    def revalidate_path(self, path: ExecutionPath) -> ExecutionPath: ...

    def display_path(self, path: ExecutionPath) -> str: ...

    def metadata(self, path: ExecutionPath) -> FileMetadata: ...

    def read_bytes(self, path: ExecutionPath, limit: int | None = None) -> bytes: ...

    def read_text(
        self, path: ExecutionPath, *, encoding: str, errors: str = "strict"
    ) -> str: ...

    def write_text(self, path: ExecutionPath, content: str, *, encoding: str) -> None: ...

    def ensure_directory(self, path: ExecutionPath) -> None: ...

    def iter_directory(self, path: ExecutionPath) -> Iterable[DirectoryEntry]: ...

    def glob(self, path: ExecutionPath, pattern: str) -> Iterable[DirectoryEntry]: ...

    def iter_search_candidates(
        self,
        path: ExecutionPath,
        *,
        glob: str | None = None,
        deadline: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Iterable[ExecutionPath]: ...

    def search_files(
        self,
        paths: Iterable[ExecutionPath],
        pattern: str,
        *,
        case_sensitive: bool = False,
        fixed_string: bool = False,
        deadline: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Iterable[SearchMatch]: ...

    def start_shell(self, command: str, *, cwd: ExecutionPath, grant: InvocationGrant) -> ProcessHandle: ...
