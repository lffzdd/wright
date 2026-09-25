"""Shared execution values independent of the local operating system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FileKind = Literal["file", "directory", "symlink", "other", "missing"]


@dataclass(frozen=True)
class ExecutionPath:
    """A canonical absolute path belonging to one execution environment."""

    environment_id: str
    value: str

    def __str__(self) -> str:
        return self.value

    @property
    def name(self) -> str:
        return self.value.rstrip("/").rsplit("/", 1)[-1]

    @property
    def parent(self) -> ExecutionPath:
        value = self.value.rstrip("/")
        parent = value.rsplit("/", 1)[0] or "/"
        return ExecutionPath(self.environment_id, parent)


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
