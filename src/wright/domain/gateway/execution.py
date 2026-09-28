"""Execution values the domain can name without owning an operating system.

Infrastructure backends implement :class:`PathResolver`. ``ExecutionPath`` is
the canonical path value those backends return and permission grants store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


class PathResolver(Protocol):
    """The path operations permission resolution is allowed to ask for.

    A full execution backend may do much more. The resolver only needs the
    current working directory and canonical path resolution.
    """

    def cwd(self) -> ExecutionPath: ...

    def resolve_path(
        self, requested: str, *, cwd: ExecutionPath | None = None
    ) -> ExecutionPath: ...
