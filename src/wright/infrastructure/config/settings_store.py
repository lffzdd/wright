"""JSON settings files: ``~/.wright/settings.json`` and ``{project}/.wright/settings.json``."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core.file_lock import FileLock
from ...core.paths import project_settings_path, user_settings_path
from ...domain.model.settings import SettingScope, SettingsError

_LOCK = threading.RLock()


class JsonSettingsStore:
    """Read and edit the settings files for one project root (or none)."""

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.project_root = project_root
        self._environ = environ

    def path(self, scope: SettingScope) -> Path | None:
        if scope == "user":
            return user_settings_path()
        if self.project_root is None:
            return None
        return project_settings_path(self.project_root)

    def read(self, scope: SettingScope) -> Mapping[str, Any]:
        target = self.path(scope)
        if target is None or not target.is_file():
            return {}
        return _load(target)

    def write(self, scope: SettingScope, key: str, value: Any) -> None:
        target = self.path(scope)
        if target is None:
            raise SettingsError("No project is open, so there is no project settings file")
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(f"{target.name}.lock")
        with _LOCK, FileLock(lock_path):
            data = dict(_load(target)) if target.is_file() else {}
            _set_key(data, key.split("."), value)
            _replace(target, data, private=scope == "user")

    def environment(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ


def _load(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    return data


def _set_key(data: dict[str, Any], parts: list[str], value: Any) -> None:
    head, rest = parts[0], parts[1:]
    if not rest:
        if value is None:
            data.pop(head, None)
        else:
            data[head] = value
        return
    child = data.get(head)
    child = dict(child) if isinstance(child, dict) else {}
    _set_key(child, rest, value)
    if child:
        data[head] = child
    else:
        data.pop(head, None)


def _replace(target: Path, data: Mapping[str, Any], *, private: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["JsonSettingsStore"]
