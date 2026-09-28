"""Text diffing and colored patch generation utilities."""

from __future__ import annotations

import difflib


def compute_unified_diff(
    old_content: str,
    new_content: str,
    fromfile: str = "before",
    tofile: str = "after",
    lineterm: str = "\n",
) -> str:
    """Generate a unified diff between old and new text contents."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm=lineterm,
    )
    return "".join(diff)


def render_colored_diff(diff_text: str) -> str:
    """Format unified diff text with ANSI escape codes for terminal display."""
    colored_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            colored_lines.append(f"\033[1m{line}\033[0m")
        elif line.startswith("@@"):
            colored_lines.append(f"\033[36m{line}\033[0m")
        elif line.startswith("+"):
            colored_lines.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-"):
            colored_lines.append(f"\033[31m{line}\033[0m")
        else:
            colored_lines.append(line)
    return "\n".join(colored_lines)
