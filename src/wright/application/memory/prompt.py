"""Static memory instructions and the per-request core-memory projection.

Only static instructions live here. The MEMORY.md index and recalled bodies
are injected per turn so they stay fresh. Core memory is replaced on the
request copy of the system prompt and is not written into history.
"""

from __future__ import annotations

from pathlib import Path

from ...domain.model.memory import (
    FRONTMATTER_EXAMPLE,
    TRUSTING_RECALL,
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE,
    WHEN_TO_ACCESS,
)
from ...infrastructure.persistence.memory import memory_dir


def project_core_memory(system_prompt: str, core_block: str) -> str:
    """Replace a legacy pinned snapshot in a request copy of the system prompt."""
    # Earlier sessions persisted the block as the system prompt's prefix.
    # Remove only that prefix, preserving role instructions and tool catalogs.
    if system_prompt.startswith("<CORE_MEMORY>\n"):
        _, closing, remainder = system_prompt.partition("\n</CORE_MEMORY>")
        if closing:
            system_prompt = remainder.lstrip("\n")
    if core_block:
        return f"{core_block}\n\n{system_prompt}"
    return system_prompt


def build_memory_instructions(directory: Path | None = None) -> str:
    """Static memory instructions appended to the system prompt."""
    directory = directory or memory_dir()

    how_to_save = f"""## How to save memory

Use the memory tools. Do not hand-edit files and do not edit `MEMORY.md`.

- `type` is the content category (user / feedback / project / reference). `scope` is where it applies. They do not replace each other.
- Write to the current project by default. Do not automatically make a user preference or a piece of feedback global.
- Pass `scope=global` to `create_memory` only when the user explicitly wants a memory that applies to every project.
- If there is no current project, a project write fails. Do not change it to global.
- Use `create_memory` for a new topic. The same title does not overwrite an existing memory. Identity is the stable id.
- Use `update_memory` to change an existing memory. Pass the id and `expected_revision` from `get_memory`. Changing the title does not change the id.
- When a memory is stale but should be kept, set `status` to `inactive`. That is not deletion, and editing the body does not reactivate it.
- Use `delete_memory` only when the user explicitly asks you to forget something.
- Search with `search_memory` before writing. The default scope is the current project plus global memories. Other projects need an explicit scope.
- The tools maintain this file format. Do not put the body in `MEMORY.md`:

{FRONTMATTER_EXAMPLE}"""

    sections = [
        "# Long-term memory",
        "",
        f"You have a persistent file-backed memory system at `{directory}`. It survives across sessions.",
        "",
        (
            "Build it over time so a later conversation can tell who the user is, how they want to "
            "collaborate, which behaviors to avoid or repeat, and the context behind the work."
        ),
        "",
        "If the user explicitly asks you to remember something, save it immediately as the closest type. If they ask you to forget something, find it and delete it with `delete_memory`.",
        "",
        TYPES_SECTION,
        WHAT_NOT_TO_SAVE,
        "",
        how_to_save,
        "",
        WHEN_TO_ACCESS,
        "",
        TRUSTING_RECALL,
        "",
        "## Core Memory",
        (
            "Core memory is a small amount of resident background reread on every model request. "
            "It is correctable long-term information and must not override an explicit instruction in the current request."
        ),
        "- `persona` is a fixed role. It does not contain an unconfirmed job, system, or project identity. Tools cannot edit or clear it.",
        "- `human_profile` holds cross-project facts and preferences for the current user, in global core memory. Do not put project constraints there.",
        "- `project_anchor` holds a few long-lived constraints for the current project, stored separately by project_id. Writing it fails when there is no project context.",
        "- `append` and `replace` require non-empty content. Clear a section with `mode=clear`. An empty string is not a clear.",
        "",
        "## Boundary with other persistence",
        (
            "Memory is for information that is still useful in a future conversation. Do not store something that matters only in this conversation. "
            "Current steps and progress belong to the task or plan. Memory is for knowledge that crosses sessions."
        ),
        (
            "The system records a history episode when each user turn ends. Do not create or edit episodes by hand. "
            "An episode is past execution experience. Verify the current state with tools before relying on it."
        ),
    ]
    return "\n".join(sections)
