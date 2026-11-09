"""List local folders so the web UI can pick a workspace directory.

Only directory names are returned; file contents are never read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_ENTRIES = 500


class FolderBrowseError(ValueError):
    pass


def browse_folders(path: str | None) -> dict[str, Any]:
    raw = (path or "").strip()
    target = Path(raw).expanduser() if raw else Path.home()
    if not target.is_absolute():
        raise FolderBrowseError("folder path must be absolute")
    resolved = target.resolve()
    if not resolved.is_dir():
        raise FolderBrowseError("folder does not exist")
    try:
        children = sorted(resolved.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        raise FolderBrowseError(f"cannot read folder: {exc.strerror or exc}") from exc
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            git = (child / ".git").exists()
        except OSError:
            continue
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        entries.append({"name": child.name, "path": str(child), "git": git})
    parent = resolved.parent
    return {
        "path": str(resolved),
        "parent": str(parent) if parent != resolved else None,
        "home": str(Path.home()),
        "git": (resolved / ".git").exists(),
        "entries": entries,
        "truncated": truncated,
    }
