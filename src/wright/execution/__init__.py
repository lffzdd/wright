"""Public execution boundary."""

from .authorized import AuthorizedExecution
from .local import LocalExecutionBackend, LocalProcessHandle
from .protocols import ExecutionBackend, ProcessHandle
from .types import (
    CompletedProcess,
    DirectoryEntry,
    ExecutionPath,
    FileMetadata,
    SearchMatch,
)

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
