"""Immutable session access scopes.

An :class:`AccessScope` answers only the question "is this resolved target in
the session's approved roots?".  It does not grant a tool operation and it
does not perform local filesystem I/O.  The execution backend owns path
canonicalisation; this module consumes the canonical strings it returns.
"""

from __future__ import annotations

import ntpath
import posixpath
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath


class PathClass(str, Enum):
    IN_ORIGIN = "in_origin"
    IN_GRANTED = "in_granted"
    OUTSIDE = "outside"
    FORBIDDEN = "forbidden"


def _absolute(path: Path | str) -> PurePath:
    """Consume canonical absolute paths without environment or filesystem reads."""
    raw = str(path)
    result = PureWindowsPath(raw) if PureWindowsPath(raw).is_absolute() else PurePosixPath(raw)
    if not result.is_absolute():
        raise ValueError("Access roots must be canonical absolute paths")
    return result


def path_module(value: str):
    return ntpath if PureWindowsPath(value).is_absolute() else posixpath


def _is_under(path: str, root: str) -> bool:
    module = path_module(root)
    try:
        return module.normcase(module.commonpath((path, root))) == module.normcase(module.normpath(root))
    except ValueError:
        return False


def resolve_root(path: Path | str) -> PurePath:
    return _absolute(path)


def is_under(path: Path | str, root: Path | str) -> bool:
    """Pure containment for canonical backend resource identities."""
    return _is_under(str(path), str(root))


@dataclass(frozen=True)
class AccessScope:
    """An immutable snapshot of the origin and session-approved roots."""

    origin: Path
    additional: tuple[Path, ...] = ()
    protected: tuple[Path, ...] = ()
    project_root: Path | None = None
    read_only: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        origin = _absolute(self.origin)
        unique: list[Path] = []
        seen = {str(origin)}
        for raw in self.additional:
            root = _absolute(raw)
            key = str(root)
            if key in seen or _is_under(key, str(origin)):
                continue
            if any(_is_under(key, str(existing)) for existing in unique):
                continue
            unique = [
                existing for existing in unique
                if not _is_under(str(existing), key)
            ]
            seen.add(key)
            unique.append(root)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "additional", tuple(unique))

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.origin, *self.additional)

    def with_additional(self, directories: tuple[Path, ...] | list[Path]) -> AccessScope:
        return AccessScope(self.origin, (*self.additional, *directories), self.protected, self.project_root, self.read_only)

    def contains(self, path: Path | str) -> bool:
        value = str(path)
        return any(_is_under(value, str(root)) for root in self.roots)

    def classify(self, path: Path | str) -> PathClass:
        value = str(path)
        if any(_is_under(value, str(forbidden)) for forbidden in self.protected):
            return PathClass.FORBIDDEN
        if _is_under(value, str(self.origin)):
            return PathClass.IN_ORIGIN
        if any(_is_under(value, str(root)) for root in self.additional):
            return PathClass.IN_GRANTED
        return PathClass.OUTSIDE
