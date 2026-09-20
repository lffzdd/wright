"""Access scope: original execution root plus extra granted directories.

This is an application-level working-directory set, not an OS sandbox.
Permission classifies a path against the set; the execution backend then
enforces the authorized roots plus any one-shot invocation paths.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from threading import Lock

from .paths import user_permission_settings_path


class PathClass(str, Enum):
    IN_ORIGIN = "in_origin"
    IN_GRANTED = "in_granted"
    OUTSIDE = "outside"
    FORBIDDEN = "forbidden"


def resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def forbidden_paths() -> tuple[Path, ...]:
    return (user_permission_settings_path().resolve(),)


class AccessScope:
    """Authorized filesystem roots for one session."""

    def __init__(
        self,
        origin: Path,
        additional: tuple[Path, ...] | list[Path] = (),
    ) -> None:
        self.origin = resolve_path(origin)
        self._lock = Lock()
        self.additional: tuple[Path, ...] = ()
        self.sync_additional(additional)

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.origin, *self.additional)

    def sync_additional(self, directories: tuple[Path, ...] | list[Path]) -> None:
        unique: list[Path] = []
        seen: set[Path] = {self.origin}
        for raw in directories:
            resolved = resolve_path(raw)
            if resolved in seen or is_under(resolved, self.origin):
                continue
            seen.add(resolved)
            unique.append(resolved)
        with self._lock:
            self.additional = tuple(unique)

    def add(self, directory: Path | str) -> Path:
        resolved = resolve_path(directory)
        with self._lock:
            if resolved == self.origin or any(
                is_under(resolved, root) for root in (self.origin, *self.additional)
            ):
                return resolved
            self.additional = (*self.additional, resolved)
        return resolved

    def contains(self, path: Path | str) -> bool:
        resolved = resolve_path(path)
        return any(is_under(resolved, root) for root in self.roots)

    def classify(self, path: Path | str) -> PathClass:
        resolved = resolve_path(path)
        for forbidden in forbidden_paths():
            if resolved == forbidden:
                return PathClass.FORBIDDEN
        if is_under(resolved, self.origin):
            return PathClass.IN_ORIGIN
        for root in self.additional:
            if is_under(resolved, root):
                return PathClass.IN_GRANTED
        return PathClass.OUTSIDE
