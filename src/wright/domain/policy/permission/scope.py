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
    # Directories granted for writing. Every other additional root is readable
    # only: a read grant adds reading and never takes writing away from another.
    writable: tuple[Path, ...] = ()

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
        object.__setattr__(self, "protected", tuple(_absolute(path) for path in self.protected))
        writable: list[Path] = []
        for raw in self.writable:
            root = _absolute(raw)
            if _is_under(str(root), str(origin)) or any(_is_under(str(root), str(existing)) for existing in writable):
                continue
            writable = [existing for existing in writable if not _is_under(str(existing), str(root))]
            writable.append(root)
        object.__setattr__(self, "writable", tuple(writable))
        if self.project_root is not None:
            object.__setattr__(self, "project_root", _absolute(self.project_root))
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "additional", tuple(unique))

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.origin, *self.additional)

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return (self.origin, *self.writable)

    @property
    def read_only(self) -> tuple[Path, ...]:
        """Additional roots that no write grant covers, for display."""
        return tuple(root for root in self.additional if not self.can_write(root))

    def with_additional(self, directories: tuple[Path, ...] | list[Path]) -> AccessScope:
        return AccessScope(self.origin, (*self.additional, *directories), self.protected, self.project_root, self.writable)

    def contains(self, path: Path | str) -> bool:
        value = str(path)
        return any(_is_under(value, str(root)) for root in self.roots)

    def can_write(self, path: Path | str) -> bool:
        value = str(path)
        return any(_is_under(value, str(root)) for root in self.writable_roots)

    def classify(self, path: Path | str) -> PathClass:
        value = str(path)
        if any(_is_under(value, str(forbidden)) for forbidden in self.protected):
            return PathClass.FORBIDDEN
        if _is_under(value, str(self.origin)):
            return PathClass.IN_ORIGIN
        if any(_is_under(value, str(root)) for root in self.additional):
            return PathClass.IN_GRANTED
        return PathClass.OUTSIDE


# How far above the nearest directory an approval may offer to grant.
_ANCESTOR_LEVELS = 3


def directory_grant_candidates(directories: tuple[str, ...], scope: AccessScope, *, home: str = "") -> tuple[str, ...]:
    """Directories an approval may offer for these targets, nearest first.

    The nearest one is the common directory of ``directories``. No candidate is
    the home directory, one of its ancestors, or the filesystem root; climbing
    also stops before a top-level directory or one that contains a protected
    path, so one click never reaches across the whole account. A chain inside
    an approved root stops at that root: above it the grant would leave the
    root the target belongs to.
    """
    if not directories:
        return ()
    module = path_module(directories[0])
    try:
        nearest = module.commonpath([module.normpath(item) for item in directories])
    except ValueError:
        return ()
    containing = [str(root) for root in scope.roots if _is_under(nearest, str(root))]
    ceiling = max(containing, key=len) if containing else ""
    candidates: list[str] = []
    current = nearest
    while len(candidates) <= _ANCESTOR_LEVELS:
        if not _offerable(current, scope, home, module, nearest=not candidates):
            break
        candidates.append(current)
        if ceiling and module.normcase(current) == module.normcase(module.normpath(ceiling)):
            break
        parent = module.dirname(current)
        if parent == current:
            break
        current = parent
    return tuple(candidates)


def _offerable(directory: str, scope: AccessScope, home: str, module, *, nearest: bool) -> bool:
    pure = PureWindowsPath(directory) if module is ntpath else PurePosixPath(directory)
    # The target's own top-level directory (such as /tmp) is still a useful
    # grant; climbing up to one is not.
    if len(pure.parts) < (2 if nearest else 3):
        return False
    if home and _is_under(home, directory):
        return False
    # Protected files stay denied inside any grant, so the target's own
    # directory is still offered; climbing past one to a wider grant is not.
    return nearest or not any(_is_under(str(protected), directory) for protected in scope.protected)
