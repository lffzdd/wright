"""Persistence implementations for agent memory subsystems (Core, Fact, Episodic)."""

from __future__ import annotations

from .core import (
    CORE_MEMORY_FILE,
    FileCoreMemoryStore,
)
from .episode import (
    EpisodeNotFoundError,
    EpisodeRecord,
    EpisodeStatus,
    EpisodeStore,
    EpisodeStoreError,
    episode_from_session,
    format_episode_manifest,
    read_episodes_for_surfacing,
)
from .fact import (
    FileFactRepository,
    MemoryStoreError,
    create_memory,
    delete_memory,
    dump_frontmatter,
    format_manifest,
    get_memory,
    normalize_memory_id,
    parse_frontmatter,
    read_entrypoint,
    read_memories_for_surfacing,
    rebuild_index,
    scan_memory_files,
    search_memories,
    slugify,
    update_memory,
    write_memory_file,
)
from .paths import (
    MEMORY_INDEX,
    ensure_memory_dir,
    entrypoint_path,
    memory_dir,
)

__all__ = [
    "CORE_MEMORY_FILE",
    "EpisodeNotFoundError",
    "EpisodeRecord",
    "EpisodeStatus",
    "EpisodeStore",
    "EpisodeStoreError",
    "FileCoreMemoryStore",
    "FileFactRepository",
    "MEMORY_INDEX",
    "MemoryStoreError",
    "create_memory",
    "delete_memory",
    "dump_frontmatter",
    "ensure_memory_dir",
    "entrypoint_path",
    "episode_from_session",
    "format_episode_manifest",
    "format_manifest",
    "get_memory",
    "memory_dir",
    "normalize_memory_id",
    "parse_frontmatter",
    "read_entrypoint",
    "read_episodes_for_surfacing",
    "rebuild_index",
    "scan_memory_files",
    "search_memories",
    "slugify",
    "update_memory",
    "write_memory_file",
]
