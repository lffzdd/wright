"""File references shared by CLI, TUI and Web.

Selection stores an identity. Content is read only when a turn starts, from
the session's execution directory, and that snapshot is what history keeps.
Searching a project does not grant permission to read file bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...core.paths import project_id
from .files import PathRejected, resolve_inside
from .search import search_files

MAX_REFERENCE_BYTES = 5 * 1024 * 1024
MAX_INLINE_CHARS = 100_000


class ReferenceError(ValueError):
    pass


def active_mention_query(text: str) -> str | None:
    """Return the unfinished ``@query`` at the end of a composer.

    This is only a search trigger. It is not a file reference. A reference is
    either a structured identity or an explicit ``@[path]`` token.
    """

    import re

    match = re.search(r"(?:^|\s)@([^\s@]*)$", text)
    if match is None:
        return None
    return match.group(1)


def parse_explicit_mentions(text: str) -> list[str]:
    """Return paths written as ``@[path]``.

    Bare ``@name`` text and ordinary email addresses are left alone. Spaces
    are accepted only inside the brackets.
    """

    found: list[str] = []
    index = 0
    while True:
        start = text.find("@[", index)
        if start < 0:
            return found
        end = text.find("]", start + 2)
        if end < 0:
            return found
        token = text[start + 2:end].strip()
        if token and "\n" not in token and "\r" not in token:
            found.append(token)
        index = end + 1


def search_file_references(project_root: Path, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """List candidate files under a project. This does not read file contents."""

    owner = project_id(project_root)
    return [
        {
            "kind": "file",
            "path": item["path"],
            "name": item["name"],
            "project_id": owner,
            "external": False,
        }
        for item in search_files(project_root, query, limit=limit)
    ]


def identify_reference(
    project_root: Path,
    raw: str,
    *,
    external_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a selected path and return its identity. Does not read bytes."""

    text = raw.strip()
    if not text:
        raise ReferenceError("reference path is empty")
    owner = project_id(project_root)
    if Path(text).is_absolute() or text.startswith("~"):
        path = Path(text).expanduser().resolve()
        if not path.is_file():
            raise ReferenceError("external reference is not a file")
        if not _under_any(path, external_roots or []):
            raise ReferenceError("external path is not in an authorized directory")
        return {
            "kind": "file",
            "path": str(path),
            "name": path.name,
            "project_id": owner,
            "external": True,
        }
    try:
        resolved = resolve_inside(project_root, text)
    except PathRejected as exc:
        raise ReferenceError(str(exc)) from exc
    if not resolved.is_file():
        raise ReferenceError("reference path is not a file")
    relative = resolved.relative_to(project_root.expanduser().resolve()).as_posix()
    return {
        "kind": "file",
        "path": relative,
        "name": resolved.name,
        "project_id": owner,
        "external": False,
    }


def prepare_submission_references(
    project_root: Path,
    references: list[Any],
    prompt: str,
    *,
    external_roots: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Turn structured refs and explicit ``@[path]`` tokens into identities."""

    prepared: list[dict[str, Any]] = []
    seen: set[tuple[bool, str]] = set()

    def add(raw: str) -> None:
        ref = identify_reference(project_root, raw, external_roots=external_roots)
        key = (bool(ref["external"]), str(ref["path"]))
        if key in seen:
            return
        seen.add(key)
        prepared.append(ref)

    for item in references:
        if isinstance(item, str):
            add(item)
            continue
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            add(str(item["path"]))
            continue
        raise ReferenceError("a file reference needs a path")
    for token in parse_explicit_mentions(prompt):
        add(token)
    return prepared


def capture_references(
    *,
    project_root: Path,
    execution_root: Path,
    references: list[dict[str, Any]],
    reader: Callable[[Path], bytes] | None = None,
    external_roots: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read each reference from the execution directory at turn start.

    A project-relative reference is never silently satisfied from the live
    checkout when the execution directory is a worktree. The returned records
    are the bytes the model was shown.
    """

    owner = project_id(project_root)
    return [
        _capture_one(execution_root, item, owner, external_roots or [], reader)
        for item in references
    ]


def render_captures(prompt: str, captures: list[dict[str, Any]]) -> str:
    """Append the execution-time snapshot to the user text."""

    blocks = [_render_capture(item) for item in captures]
    blocks = [block for block in blocks if block]
    if not blocks:
        return prompt
    body = "\n\n".join(blocks)
    if prompt.strip():
        return f"{prompt.rstrip()}\n\n{body}"
    return body


def _capture_one(
    execution_root: Path,
    ref: dict[str, Any],
    owner: str,
    external_roots: list[str],
    reader: Callable[[Path], bytes] | None = None,
) -> dict[str, Any]:
    base = {
        "kind": "file",
        "path": str(ref.get("path") or ""),
        "name": str(ref.get("name") or Path(str(ref.get("path") or "")).name),
        "project_id": str(ref.get("project_id") or ""),
        "external": bool(ref.get("external")),
        "sha256": None,
        "size": None,
        "text": None,
    }
    if base["project_id"] and base["project_id"] != owner:
        return {**base, "status": "denied", "reason": "reference belongs to another project"}
    if base["external"]:
        path = Path(base["path"]).expanduser().resolve()
        if not _under_any(path, external_roots):
            return {**base, "status": "denied", "reason": "external path is not authorized"}
        target = path
    else:
        try:
            target = resolve_inside(execution_root, base["path"])
        except PathRejected as exc:
            return {**base, "status": "denied", "reason": str(exc)}
    if not target.is_file():
        return {
            **base,
            "status": "missing",
            "reason": "file was not in the execution directory when the turn started",
        }
    try:
        if reader is None:
            digest, size = _hash_file(target)
            raw = target.read_bytes() if size <= MAX_REFERENCE_BYTES else b""
        else:
            raw = reader(target)
            size = len(raw)
            digest = hashlib.sha256(raw).hexdigest()
    except PermissionError as exc:
        return {**base, "status": "denied", "reason": str(exc)}
    except OSError as exc:
        return {**base, "status": "missing", "reason": str(exc)}
    recorded = {**base, "sha256": digest, "size": size, "name": target.name}
    if size > MAX_REFERENCE_BYTES:
        return {**recorded, "status": "too_large", "reason": "file exceeds 5 MiB"}
    if b"\0" in raw[:8192]:
        return {**recorded, "status": "binary"}
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_INLINE_CHARS:
        return {
            **recorded,
            "status": "truncated",
            "text": text[:MAX_INLINE_CHARS],
            "reason": "content was truncated at execution time",
        }
    return {**recorded, "status": "text", "text": text}


def _render_capture(item: dict[str, Any]) -> str:
    path = item.get("path") or item.get("name") or "file"
    digest = item.get("sha256") or ""
    status = item.get("status")
    if status == "text":
        return f"[file {path} sha256={digest} captured]\n{item.get('text') or ''}"
    if status == "truncated":
        return f"[file {path} sha256={digest} captured truncated]\n{item.get('text') or ''}"
    if status == "binary":
        return f"[file {path} sha256={digest} binary size={item.get('size')}]"
    if status == "too_large":
        return f"[file {path} sha256={digest} too_large size={item.get('size')}]"
    if status == "missing":
        return f"[file {path} missing when the turn started]"
    if status == "denied":
        return f"[file {path} not read: {item.get('reason') or 'not authorized'}]"
    return ""


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _under_any(path: Path, roots: list[str]) -> bool:
    resolved = path.expanduser().resolve()
    for root in roots:
        base = Path(root).expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return True
    return False


__all__ = [
    "MAX_INLINE_CHARS",
    "MAX_REFERENCE_BYTES",
    "ReferenceError",
    "active_mention_query",
    "capture_references",
    "identify_reference",
    "parse_explicit_mentions",
    "prepare_submission_references",
    "render_captures",
    "search_file_references",
]
