"""File, task, content, and syntactic symbol search.

Symbol results come from language-specific definition syntax. A content
search is labeled separately and is not returned as a symbol.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from .files import _SKIP_DIRS, resolve_inside

MAX_FILES = 4_000
MAX_MATCHES = 100
MAX_FILE_BYTES = 1_000_000

_DEFINITION = {
    ".go": re.compile(
        r"^(?:func\s+(?:\([^)]*\)\s*)?(\w+)|type\s+(\w+)\s+(?:struct|interface))",
        re.MULTILINE,
    ),
    ".rs": re.compile(
        r"^(?:(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|type)\s+(\w+))",
        re.MULTILINE,
    ),
}
_JS = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|class\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\())",
    re.MULTILINE,
)


def search_files(root: Path, query: str, *, limit: int = 50) -> list[dict[str, str]]:
    needle = query.strip().lower()
    if not needle:
        return []
    matches: list[dict[str, str]] = []
    root_resolved = root.expanduser().resolve()
    for path in _iter_files(root_resolved):
        relative = path.relative_to(root_resolved).as_posix()
        if needle in relative.lower() or needle in path.name.lower():
            matches.append({"path": relative, "name": path.name, "kind": "file"})
            if len(matches) >= limit:
                break
    return matches


def search_content(root: Path, query: str, *, limit: int = 50) -> list[dict[str, object]]:
    needle = query.strip()
    if not needle:
        return []
    matches: list[dict[str, object]] = []
    root_resolved = root.expanduser().resolve()
    lowered = needle.lower()
    for path in _iter_files(root_resolved):
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if lowered in line.lower():
                matches.append({
                    "path": path.relative_to(root_resolved).as_posix(),
                    "line": line_no,
                    "kind": "content",
                    "text": line[:240],
                })
                if len(matches) >= limit:
                    return matches
    return matches


def search_symbols(root: Path, query: str, *, limit: int = 50) -> list[dict[str, object]]:
    """Return definition sites whose names match. This does not scan prose."""

    needle = query.strip().lower()
    if not needle:
        return []
    matches: list[dict[str, object]] = []
    root_resolved = root.expanduser().resolve()
    for path in _iter_files(root_resolved):
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        for symbol in _definitions(path):
            if needle in symbol["name"].lower():
                matches.append({
                    **symbol,
                    "path": path.relative_to(root_resolved).as_posix(),
                    "kind": "symbol",
                })
                if len(matches) >= limit:
                    return matches
    return matches


def search_tasks(sessions: list[dict[str, object]], query: str, *, limit: int = 50) -> list[dict[str, object]]:
    needle = query.strip().lower()
    if not needle:
        return []
    found: list[dict[str, object]] = []
    for item in sessions:
        label = str(item.get("user_goal") or item.get("session_label") or "")
        session_id = str(item.get("session_id") or "")
        if needle in label.lower() or needle in session_id.lower():
            found.append({
                "kind": "task",
                "session_id": session_id,
                "title": label,
                "status": item.get("status"),
                "project_root": item.get("project_root"),
            })
            if len(found) >= limit:
                break
    return found


def search_within(root: Path, relative_root: str = "") -> Path:
    return resolve_inside(root, relative_root)


def _iter_files(root: Path):
    seen = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            seen += 1
            if seen > MAX_FILES:
                return
            yield Path(directory) / name


def _definitions(path: Path) -> list[dict[str, object]]:
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    if suffix == ".py":
        return _python_definitions(text)
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return _pattern_definitions(text, _JS)
    pattern = _DEFINITION.get(suffix)
    if pattern is None:
        return []
    return _pattern_definitions(text, pattern)


def _python_definitions(text: str) -> list[dict[str, object]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            found.append({"name": node.name, "symbol_kind": kind, "line": node.lineno})
    return found


def _pattern_definitions(text: str, pattern: re.Pattern[str]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for match in pattern.finditer(text):
        name = next((group for group in match.groups() if group), "")
        if not name:
            continue
        line = text.count("\n", 0, match.start()) + 1
        header = match.group(0)
        if header.lstrip().startswith(("class ", "export class", "type ", "struct ", "enum ", "trait ")):
            symbol_kind = "type"
        else:
            symbol_kind = "function"
        found.append({"name": name, "symbol_kind": symbol_kind, "line": line})
    return found


__all__ = [
    "search_content",
    "search_files",
    "search_symbols",
    "search_tasks",
    "search_within",
]
