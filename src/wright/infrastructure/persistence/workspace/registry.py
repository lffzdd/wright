"""Registered project directories.

The selected id is a UI preference. It is not the process working directory
and it is not the execution root of a running session.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ....core.paths import project_id, workspace_registry_path

_LOCK = threading.RLock()


def load_registry(path: Path | None = None) -> dict:
    target = path or workspace_registry_path()
    if not target.is_file():
        return {"selected_project_id": None, "projects": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"selected_project_id": None, "projects": []}
    if not isinstance(data, dict):
        return {"selected_project_id": None, "projects": []}
    projects = data.get("projects")
    if not isinstance(projects, list):
        projects = []
    return {
        "selected_project_id": data.get("selected_project_id"),
        "projects": [item for item in projects if isinstance(item, dict)],
    }


def save_registry(data: dict, path: Path | None = None) -> None:
    target = path or workspace_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with _LOCK:
        import fcntl

        with lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
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


def identity_for(root: Path) -> str:
    return project_id(root)


__all__ = ["identity_for", "load_registry", "save_registry"]
