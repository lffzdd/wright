"""System instructions. Tool inventories are derived from the assembled catalog."""

from __future__ import annotations

from collections.abc import Sequence

from .model.tool import ModelVisibleTool, split_tool_catalog

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
    tools: Sequence[ModelVisibleTool],
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
    if {"edit", "write"} <= names:
        edit_advice = (
            "Prefer edit for unique in-place replacements; set replace_all to "
            "replace every occurrence. Use write to create a file or rewrite it whole."
        )
        if "read" in names:
            edit_advice = (
                "Prefer edit for unique in-place replacements; set replace_all to "
                "replace every occurrence. Read the file with read before edit "
                "or before overwriting it with write. read prefixes each line "
                "with N|; do not copy those prefixes into edit old_text. Use "
                "write to create a file or rewrite it whole."
            )
        paragraphs.append(edit_advice)
    if "shell" in names:
        shell = (
            "shell keeps a working directory across calls and returns cwd "
            "in every result. Do not cd into the directory you are already in."
        )
        if "shell_output" in names:
            shell += (
                " Background commands return command_id. Use shell_output to read "
                "or wait for that execution and shell_kill to stop it."
            )
        paragraphs.append(shell)
    if deferred:
        paragraphs.append(
            "Specialized tools are listed with one sentence and no parameters. "
            "Before first using one, call tool_search with its name to get the "
            "full schema, then call it by name. Tools tool_search marks "
            "invoke=call_tool are not listed; run them with call_tool. Do not "
            "search for tools whose schema you already have."
        )
    paragraphs.append(
        "Base claims about completed work on actual tool results. Continue after "
        "tool results until the task is complete or requires user input. Respond "
        "directly to the user in natural language."
    )
    paragraphs.append(
        "Follow the user's language. By default, match the language of the user's "
        "latest message. If the user asks for a specific language, use that language. "
        "Do not change the reply language because of tool schemas, system reminders, "
        "or the application's interface language."
    )
    prompt = "\n\n".join(paragraphs) + "\n"
    if memory_section:
        return f"{prompt}\n{memory_section}\n"
    return prompt


# Opening words of the tool-usage paragraphs ``build_system_prompt`` writes.
_TOOL_GUIDANCE = ("Always available:", "Prefer edit", "shell keeps", "Specialized tools")


def system_prompt_sections(prompt: str) -> list[tuple[str, str]]:
    """Split a prompt from ``build_system_prompt`` into named sections.

    The first paragraph is the role, the tool paragraphs are ``tools``, the
    remaining plain paragraphs are ``rules``. From the first Markdown heading
    on, such as the memory instructions, each top-level heading starts a
    section named by its title. Empty sections are left out.
    """
    sections: dict[str, list[str]] = {"role": [], "tools": [], "rules": []}
    heading = ""
    first = True
    for part in (part.strip() for part in prompt.split("\n\n")):
        if not part:
            continue
        if part.startswith("# "):
            heading = part.splitlines()[0][2:].strip() or "Instructions"
            sections.setdefault(heading, [])
        if heading:
            sections[heading].append(part)
        elif first:
            sections["role"].append(part)
        elif part.startswith(_TOOL_GUIDANCE):
            sections["tools"].append(part)
        else:
            sections["rules"].append(part)
        first = False
    return [(name, "\n\n".join(parts)) for name, parts in sections.items() if parts]


__all__ = [
    "DEFAULT_CODING_ROLE",
    "DEFAULT_GENERAL_ROLE",
    "build_system_prompt",
    "get_role_instruction",
    "system_prompt_sections",
]
