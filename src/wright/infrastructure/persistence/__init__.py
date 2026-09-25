"""Persistence adapters for episodic and semantic memory and durable storage."""

from __future__ import annotations

from .autonomy_store import (
    AutonomyNotFoundError,
    AutonomyStore,
    AutonomyStoreError,
)
from .episode_store import (
    EpisodeNotFoundError,
    EpisodeRecord,
    EpisodeStatus,
    EpisodeStore,
    EpisodeStoreError,
    episode_from_session,
    format_episode_manifest,
    read_episodes_for_surfacing,
)
from .memory_paths import (
    MEMORY_INDEX,
    ensure_memory_dir,
    entrypoint_path,
    memory_dir,
)
from .memory_store import (
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

__all__ = [
    "AutonomyNotFoundError",
    "AutonomyStore",
    "AutonomyStoreError",
    "EpisodeNotFoundError",
    "EpisodeRecord",
    "EpisodeStatus",
    "EpisodeStore",
    "EpisodeStoreError",
    "MEMORY_INDEX",
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
