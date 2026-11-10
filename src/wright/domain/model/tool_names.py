"""Canonical names of built-in tools and the names they replaced.

Older sessions, permission files, hook matchers and skills may still carry a
legacy name. Every input boundary maps it through ``canonical_tool_name``;
only canonical names are emitted. Matching code compares canonical names, so a
legacy rule keeps exactly the scope it had and never silently stops applying.
"""

from __future__ import annotations

READ = "read"
WRITE = "write"
EDIT = "edit"
LIST = "ls"
SHELL = "shell"

LEGACY_TOOL_NAMES: dict[str, str] = {
    "read_file": READ,
    "write_file": WRITE,
    "edit_file": EDIT,
    "list_directory": LIST,
    "execute_command": SHELL,
    "get_command": "shell_output",
    "wait_command": "shell_output",
    "terminate_command": "shell_kill",
}


def canonical_tool_name(name: str) -> str:
    """Return the current name for ``name``; unknown names pass through."""
    return LEGACY_TOOL_NAMES.get(name, name)


def legacy_tool_names(name: str) -> tuple[str, ...]:
    """Legacy names that now resolve to canonical ``name``."""
    return tuple(old for old, new in LEGACY_TOOL_NAMES.items() if new == name)


__all__ = [
    "EDIT",
    "LEGACY_TOOL_NAMES",
    "LIST",
    "READ",
    "SHELL",
    "WRITE",
    "canonical_tool_name",
    "legacy_tool_names",
]
