"""Diff generator utilities (alias for string_diff)."""

from __future__ import annotations

from .string_diff import compute_unified_diff, render_colored_diff

# Direct aliases for blueprint alignment
generate_diff = compute_unified_diff

__all__ = [
    "compute_unified_diff",
    "generate_diff",
    "render_colored_diff",
]
