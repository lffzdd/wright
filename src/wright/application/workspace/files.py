"""Path checks for workspace file reads.

The client supplies a relative path. The root is chosen by the server from
a registered project or from a session execution root.
"""

from __future__ import annotations

from pathlib import Path

MAX_LIST_ENTRIES = 500
MAX_READ_CHARS = 200_000
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
})


class PathRejected(ValueError):
    """The requested path is not inside the chosen root."""


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` and require the real path to stay under ``root``."""

    root_resolved = root.expanduser().resolve()
    if not root_resolved.is_dir():
        raise PathRejected("workspace root does not exist")
    raw = relative.replace("\\", "/").strip()
    if not raw or raw == ".":
        return root_resolved
    if raw.startswith(("/", "~")):
        raise PathRejected("absolute paths are not accepted")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise PathRejected("path escapes the workspace root")
    candidate = root_resolved.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise PathRejected("path cannot be resolved") from exc
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathRejected("path escapes the workspace root")
    return resolved


def list_directory(root: Path, relative: str = "") -> dict[str, object]:
    directory = resolve_inside(root, relative)
    if not directory.is_dir():
        raise PathRejected("path is not a directory")
    entries: list[dict[str, object]] = []
    truncated = False
    try:
        children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        raise PathRejected("directory cannot be listed") from exc
    for child in children:
        if child.name in _SKIP_DIRS:
            continue
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        try:
            stat = child.stat()
        except OSError:
            continue
        kind = "dir" if child.is_dir() else "file"
        entries.append({
            "name": child.name,
            "path": child.relative_to(root.expanduser().resolve()).as_posix(),
            "kind": kind,
            "size": stat.st_size if kind == "file" else None,
        })
    rel = directory.relative_to(root.expanduser().resolve()).as_posix()
    return {
        "path": "" if rel == "." else rel,
        "entries": entries,
        "truncated": truncated,
    }


def read_text(root: Path, relative: str) -> dict[str, object]:
    path = resolve_inside(root, relative)
    if not path.is_file():
        raise PathRejected("path is not a file")
    size = path.stat().st_size
    raw = path.read_bytes()
    binary = b"\0" in raw[:8192]
    if binary:
        return {
            "path": path.relative_to(root.expanduser().resolve()).as_posix(),
            "binary": True,
            "truncated": True,
            "size": size,
            "content": "",
        }
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > MAX_READ_CHARS
    if truncated:
        text = text[:MAX_READ_CHARS]
    return {
        "path": path.relative_to(root.expanduser().resolve()).as_posix(),
        "binary": False,
        "truncated": truncated,
        "size": size,
        "line_count": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "content": text,
    }


__all__ = ["MAX_LIST_ENTRIES", "MAX_READ_CHARS", "PathRejected", "list_directory", "read_text", "resolve_inside"]
