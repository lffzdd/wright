"""System instructions. Tool inventories are derived from the assembled catalog."""

from __future__ import annotations

from collections.abc import Sequence

from ..infrastructure.tools.base import Tool, split_tool_catalog

DEFAULT_CODING_ROLE = "You are a coding assistant. Use the tools provided this turn."
DEFAULT_GENERAL_ROLE = (
    "You are a versatile, analytical, and helpful general research assistant. "
    "Use the tools provided this turn to gather facts, synthesize insights, and assist the user."
)


def get_role_instruction(mode: str = "coding") -> str:
    """Return default role instruction for given agent mode."""
    if mode == "general":
        return DEFAULT_GENERAL_ROLE
    return DEFAULT_CODING_ROLE


def build_system_prompt(
    tools: Sequence[Tool],
    memory_section: str = "",
    role_instruction: str = "",
) -> str:
    baseline, deferred = split_tool_catalog(tools)
    names = set(baseline)
    paragraphs = []
    role = role_instruction.strip() if role_instruction else DEFAULT_CODING_ROLE
    paragraphs.append(role)

    if baseline:
        paragraphs.append("Always available: " + ", ".join(baseline) + ".")
    if {"edit_file", "write_file"} <= names:
        edit_advice = (
            "Prefer edit_file for unique in-place replacements; set replace_all to "
            "replace every occurrence. Use write_file to create a file or rewrite it whole."
        )
        if "read_file" in names:
            edit_advice = (
                "Prefer edit_file for unique in-place replacements; set replace_all to "
                "replace every occurrence. Read the file with read_file before edit_file "
                "or before overwriting it with write_file. read_file prefixes each line "
                "with N|; do not copy those prefixes into edit_file old_text. Use "
                "write_file to create a file or rewrite it whole."
            )
        paragraphs.append(edit_advice)
    if "execute_command" in names:
        paragraphs.append(
            "execute_command keeps a working directory across calls and returns cwd "
            "in every result. Do not cd into the directory you are already in."
        )
    if deferred:
        paragraphs.append(
            "If a specialized capability is missing this turn, call tool_search "
            "first, then use it on the next turn. Do not search for tools already "
            "in this schema."
        )
    paragraphs.append(
        "Base claims about completed work on actual tool results. Continue after "
        "tool results until the task is complete or requires user input. Respond "
        "directly to the user in natural language."
    )
    prompt = "\n\n".join(paragraphs) + "\n"
    if memory_section:
        return f"{prompt}\n{memory_section}\n"
    return prompt


__all__ = [
    "DEFAULT_CODING_ROLE",
    "DEFAULT_GENERAL_ROLE",
    "build_system_prompt",
    "get_role_instruction",
]
