"""Immutable session access scopes.

An :class:`AccessScope` answers only the question "is this resolved target in
the session's approved roots?".  It does not grant a tool operation and it
does not perform local filesystem I/O.  The execution backend owns path
canonicalisation; this module consumes the canonical strings it returns.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..paths import user_permission_settings_path


class PathClass(str, Enum):
    IN_ORIGIN = "in_origin"
    IN_GRANTED = "in_granted"
    OUTSIDE = "outside"
    FORBIDDEN = "forbidden"


def _absolute(path: Path | str) -> Path:
    """Normalize a root without following symlinks."""

    return Path(path).expanduser().absolute()


def _is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def resolve_root(path: Path | str) -> Path:
    return _absolute(path)


def is_under(path: Path | str, root: Path | str) -> bool:
    """Pure string containment helper for already-resolved path values."""

    return _is_under(str(_absolute(path)), str(_absolute(root)))


def forbidden_paths() -> tuple[Path, ...]:
    paths = [user_permission_settings_path().absolute()]
    configured = os.getenv("WRIGHT_PERMISSION_CONFIG", "").strip()
    if configured:
        paths.append(Path(configured).expanduser().absolute())
    return tuple(dict.fromkeys(paths))


@dataclass(frozen=True)
class AccessScope:
    """An immutable snapshot of the origin and session-approved roots."""

    origin: Path
    additional: tuple[Path, ...] = ()

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
        return AccessScope(self.origin, (*self.additional, *directories))

    def contains(self, path: Path | str) -> bool:
        value = str(path)
        return any(_is_under(value, str(root)) for root in self.roots)

    def classify(self, path: Path | str) -> PathClass:
        value = str(path)
        if any(value == str(forbidden) for forbidden in forbidden_paths()):
            return PathClass.FORBIDDEN
        if _is_under(value, str(self.origin)):
            return PathClass.IN_ORIGIN
        if any(_is_under(value, str(root)) for root in self.additional):
            return PathClass.IN_GRANTED
        return PathClass.OUTSIDE
