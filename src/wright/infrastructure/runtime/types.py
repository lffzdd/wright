"""Shared execution values independent of the local operating system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...domain.gateway.execution import ExecutionPath

FileKind = Literal["file", "directory", "symlink", "other", "missing"]

__all__ = [
    "CompletedProcess",
    "DirectoryEntry",
    "ExecutionPath",
    "FileKind",
    "FileMetadata",
    "SearchMatch",
]


@dataclass(frozen=True)
class FileMetadata:
    kind: FileKind
    size: int = 0
    modified_ns: int = 0


@dataclass(frozen=True)
class DirectoryEntry:
    path: ExecutionPath
    metadata: FileMetadata


@dataclass(frozen=True)
class SearchMatch:
    path: ExecutionPath
    line: int
    column: int
    text: str


@dataclass(frozen=True)
class CompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""
