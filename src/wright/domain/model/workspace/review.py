"""Classify one file head and decide whether a batch may change the disk.

Edits made by tools are already on disk. Accept records a review. Revert
restores the bytes from before this task first touched the file. A hash
mismatch means something else wrote the file after that edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Disposition = Literal[
    "preexisting",
    "task",
    "accepted",
    "external",
    "conflict",
    "unsafe",
]


@dataclass(frozen=True)
class ChangeHead:
    path: str
    kind: Literal["add", "modify", "delete"]
    origin: Literal["preexisting", "task"]
    review: Literal["pending", "accepted"]
    reversible: bool
    before_sha256: str | None
    after_sha256: str | None


def disposition(
    head: ChangeHead,
    *,
    current_sha256: str | None,
    exists: bool,
) -> Disposition:
    """Return the live state of one recorded head.

    ``current_sha256`` is the file's hash now. ``None`` means the path is
    absent. Preexisting rows compare against the hash captured at task start.
    Task rows compare against the hash of the last task write.
    """

    expected = head.after_sha256
    if head.origin == "preexisting":
        return "preexisting" if current_sha256 == expected else "external"
    if head.kind == "delete":
        if exists or current_sha256 is not None:
            return "conflict"
        if not head.reversible:
            return "unsafe"
        return "accepted" if head.review == "accepted" else "task"
    if not head.reversible:
        if current_sha256 != expected:
            return "conflict"
        return "unsafe"
    if current_sha256 != expected:
        return "conflict"
    return "accepted" if head.review == "accepted" else "task"


def preflight(action: Literal["accept", "revert"], states: list[tuple[str, Disposition]]) -> list[dict[str, str]]:
    """Check every path before any path is changed.

    Accept marks a pending task edit as reviewed. Revert is allowed for a
    pending or already reviewed task edit whose bytes still match. Neither
    action treats a preexisting or conflicting path as success.
    """

    results: list[dict[str, str]] = []
    for path, state in states:
        if action == "accept" and state == "task":
            results.append({"path": path, "state": state, "result": "ready"})
        elif action == "accept" and state == "accepted":
            results.append({"path": path, "state": state, "result": "already_accepted"})
        elif action == "revert" and state in {"task", "accepted"}:
            results.append({"path": path, "state": state, "result": "ready"})
        else:
            results.append({"path": path, "state": state, "result": "blocked"})
    return results
