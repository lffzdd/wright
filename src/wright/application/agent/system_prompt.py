"""The system prompt, rendered from the tools and memory an agent has now.

A session stores the prompt it started with, but every request renders a
fresh one: a resumed session may have other tools, or memory turned off, and
the model must not be told about tools it no longer has. The rendering is
deterministic, so the provider's prompt cache still matches between requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...domain.prompt import build_system_prompt
from ..tool_execution.tool_search import DISCOVERY_TOOL_NAMES
from .components import PreparedTools

if TYPE_CHECKING:
    from ..memory import MemoryManager


def render_system_prompt(
    prepared: PreparedTools,
    memory: MemoryManager | None,
    role_instruction: str = "",
) -> str:
    """Static instructions for these tools; the resident memory block is added per request."""
    prompt_tools = [
        tool for tool in prepared.tools
        if tool.name not in DISCOVERY_TOOL_NAMES and tool.list_in_system_prompt
    ]
    return build_system_prompt(
        prompt_tools,
        memory_section=memory.instructions() if memory else "",
        role_instruction=role_instruction or getattr(prepared.profile, "role_instruction", ""),
    )


__all__ = ["render_system_prompt"]
