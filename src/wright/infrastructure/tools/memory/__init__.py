"""Model-facing tools for agent memory subsystems (Core, Fact, Episodic)."""

from __future__ import annotations

from .core_tools import (
    build_core_memory_tools,
    get_core_memory,
    update_core_memory,
)
from .episode_tools import (
    build_episode_tools,
    delete_episode,
    get_episode,
    search_episodes,
)
from .fact_tools import (
    build_memory_tools,
    create_memory,
    delete_memory,
    get_memory,
    search_memory,
    update_memory,
)

# Alias for domain naming symmetry
build_fact_tools = build_memory_tools

__all__ = [
    "build_core_memory_tools",
    "build_episode_tools",
    "build_fact_tools",
    "build_memory_tools",
    "create_memory",
    "delete_episode",
    "delete_memory",
    "get_core_memory",
    "get_episode",
    "get_memory",
    "search_episodes",
    "search_memory",
    "update_core_memory",
    "update_memory",
]
