"""Explain failed commands the OS sandbox probably stopped.

Output text is the only signal a sandboxed process leaves, so this stays a
hint: it never changes the command's outcome and grants nothing by itself.
"""

from __future__ import annotations

import os
import re

_DENIAL = r"(?:Operation not permitted|Read-only file system|Access is denied)"
_PATH_PATTERNS = (
    re.compile(rf"(?:^|:\s)['\"]?(/[^\n:'\"]+)['\"]?:\s*{_DENIAL}", re.MULTILINE),
    re.compile(rf"{_DENIAL}:\s*['\"](/[^\n'\"]+)['\"]"),
)
_ANY_DENIAL = re.compile(_DENIAL)


def describe_sandbox_denial(output: str, grant) -> dict | None:
    """Return the hint payload, or None when the sandbox is not the likely cause."""
    if grant is None or getattr(grant, "execution_mode", "") != "sandbox":
        return None
    if not _ANY_DENIAL.search(output):
        return None
    readable = [path.value for path in grant.shell_readable]
    writable = [path.value for path in grant.shell_writable]
    blocked = [path.value for path in grant.blocked_paths]
    path = _denied_path(output)
    grant_directory = ""
    if path:
        # A path the sandbox already allows failed for some other reason, such as file flags.
        if _inside(path, writable) and not _inside(path, blocked):
            return None
        if not _inside(path, readable) and not _inside(path, blocked):
            candidate = path if os.path.isdir(path) else os.path.dirname(path)
            grant_directory = "" if candidate == os.path.dirname(candidate) else candidate
    return {"path": path, "grant_directory": grant_directory, "readable": readable, "writable": writable}


def sandbox_denial_message(denial: dict) -> str:
    target = denial["path"] or "A path this command used"
    return (
        f"{target} was probably blocked by the OS sandbox, not by command approval. "
        "Approving a command does not widen the sandbox. "
        f"Readable: {', '.join(denial['readable'])}. Writable: {', '.join(denial['writable']) or 'none'}. "
        "Do not retry through other paths outside these directories; ask the user to authorize "
        "the directory or switch this session to host mode."
    )


def _denied_path(output: str) -> str:
    for pattern in _PATH_PATTERNS:
        match = pattern.search(output)
        if match:
            return os.path.realpath(match.group(1).strip())
    return ""


def _inside(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in map(os.path.realpath, roots))
