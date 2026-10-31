"""Runtime execution backends and contracts."""

from ...domain.gateway.runtime_protocols import ExecutionBackend, ProcessHandle
from ...domain.gateway.runtime_types import (
    CompletedProcess,
    DirectoryEntry,
    ExecutionPath,
    FileMetadata,
    SearchMatch,
)
from .authorized import AuthorizedExecution
from .local import LocalExecutionBackend, LocalProcessHandle

__all__ = [
    "AuthorizedExecution",
    "CompletedProcess",
    "DirectoryEntry",
    "ExecutionBackend",
    "ExecutionPath",
    "FileMetadata",
    "LocalExecutionBackend",
    "LocalProcessHandle",
    "ProcessHandle",
    "SearchMatch",
]
