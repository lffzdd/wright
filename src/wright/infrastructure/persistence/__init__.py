"""Persistence adapters for episode and semantic memory and durable storage."""

from __future__ import annotations

from .autonomy_store import (
    AutonomyNotFoundError,
    AutonomyStore,
    AutonomyStoreError,
)
from .memory import (
    CORE_MEMORY_FILE,
    MEMORY_INDEX,
    EpisodeNotFoundError,
    EpisodeRecord,
    EpisodeStatus,
    EpisodeStore,
    EpisodeStoreError,
    FileCoreMemoryStore,
    SemanticMemoryStore,
    create_memory,
    delete_memory,
    ensure_memory_dir,
    entrypoint_path,
    get_memory,
    memory_dir,
    rebuild_index,
    search_memories,
    update_memory,
    write_memory_file,
)
from .session.errors import CheckpointError
from .session.repository import FileSessionRepository

__all__ = [
    "CORE_MEMORY_FILE",
    "MEMORY_INDEX",
    "AutonomyNotFoundError",
    "AutonomyStore",
    "AutonomyStoreError",
    "CheckpointError",
    "EpisodeNotFoundError",
    "EpisodeRecord",
    "EpisodeStatus",
    "EpisodeStore",
    "EpisodeStoreError",
    "FileCoreMemoryStore",
    "FileSessionRepository",
    "SemanticMemoryStore",
    "create_memory",
    "delete_memory",
    "ensure_memory_dir",
    "entrypoint_path",
    "get_memory",
    "memory_dir",
    "rebuild_index",
    "search_memories",
    "update_memory",
    "write_memory_file",
]
