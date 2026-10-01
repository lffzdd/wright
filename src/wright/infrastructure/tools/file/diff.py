"""Bounded display diff from the actual contents of a successful edit."""

from __future__ import annotations

from difflib import unified_diff

MAX_DIFF_BYTES = 64 * 1024
_TRUNCATION_MARKER = "\\ Diff truncated (64 KiB display limit)\n"


def edit_diff(before: str, after: str, file: str) -> dict:
    rows: list[str] = []
    size = additions = deletions = 0
    truncated = False
    in_hunk = False
    for line in unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{file}",
        tofile=f"b/{file}",
        n=3,
    ):
        if line.startswith("@@ "):
            in_hunk = True
        elif in_hunk:
            additions += line.startswith("+")
            deletions += line.startswith("-")
        # Normalize display line endings; retain a missing-newline marker.
        row = line.rstrip("\r\n") + "\n"
        if in_hunk and line[:1] in {"+", "-", " "} and not line.endswith(("\n", "\r")):
            row += "\\ No newline at end of file\n"
        row_size = len(row.encode("utf-8"))
        if not truncated and size + row_size <= MAX_DIFF_BYTES:
            rows.append(row)
            size += row_size
        else:
            truncated = True
    if truncated:
        marker_size = len(_TRUNCATION_MARKER.encode("utf-8"))
        while rows and size + marker_size > MAX_DIFF_BYTES:
            size -= len(rows.pop().encode("utf-8"))
        rows.append(_TRUNCATION_MARKER)
    return {
        "diff": "".join(rows),
        "additions": additions,
        "deletions": deletions,
        "diff_truncated": truncated,
    }
