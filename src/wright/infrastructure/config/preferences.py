"""Application preferences stored under the Wright home directory.

Interface language is process-wide UI state. It is not part of a project,
a session, or a model request.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ...core.paths import user_preferences_path

_LOCK = threading.RLock()
_KEY = "interface_language"


def load_preferences(path: Path | None = None) -> dict:
    """Return the raw preference object. Missing or unreadable files are empty."""
    target = path or user_preferences_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def preference_value(key: str, path: Path | None = None):
    value = load_preferences(path).get(key)
    return value


def save_preference(key: str, value, path: Path | None = None) -> None:
    """Merge one key and replace the file atomically."""

    def update(data: dict) -> None:
        data[key] = value

    _update(update, path)


def _update(update, path: Path | None) -> None:
    target = path or user_preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with _LOCK:
        with lock_path.open("a+") as lock_handle:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                data = load_preferences(target)
                update(data)
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
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, target)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


__all__ = ["load_preferences", "preference_value", "save_preference"]
