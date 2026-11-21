"""API keys in ``~/.wright/.env``.

Edits keep every other line and comment, write the file atomically with
owner-only permissions, and update the process environment so the next
session sees the change without a restart.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from dotenv import dotenv_values

from ...core.file_lock import FileLock
from ...core.paths import wright_home
from .files import replace_text

_LOCK = threading.RLock()


class DotenvSecretStore:
    def __init__(self, path: Path | None = None, *, environ: MutableMapping[str, str] | None = None) -> None:
        self._path = path
        self._environ = environ

    def path(self) -> Path:
        return self._path or wright_home() / ".env"

    def stored(self) -> Mapping[str, str]:
        target = self.path()
        if not target.is_file():
            return {}
        return {name: value for name, value in dotenv_values(target).items() if value}

    def environment(self) -> MutableMapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def write(self, name: str, value: str | None) -> None:
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, FileLock(target.with_name(f"{target.name}.lock")):
            lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
            _replace(target, _edit(lines, name, value))
        environ = self.environment()
        if value is None:
            environ.pop(name, None)
        else:
            environ[name] = value


def _edit(lines: list[str], name: str, value: str | None) -> list[str]:
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    kept = [line for line in lines if not assignment.match(line)]
    if value is not None:
        # Single quotes keep the value literal; the service refuses quotes and newlines.
        kept.append(f"{name}='{value}'")
    return kept


def _replace(target: Path, lines: list[str]) -> None:
    replace_text(target, "\n".join(lines) + ("\n" if lines else ""), mode=0o600)


__all__ = ["DotenvSecretStore"]
