"""Session checkpoint facade and error definitions.

The file-backed session repository implementation has moved to:
wright.infrastructure.persistence.file_session_repo
"""

from __future__ import annotations

from ..infrastructure.persistence.file_session_repo import (
    CHECKPOINT_VERSION,
    CheckpointError,
    FileSessionRepository,
    SessionCheckpointStore,
    _deserialize_session,
    _serialize_session,
)

__all__ = [
    "CHECKPOINT_VERSION",
    "CheckpointError",
    "FileSessionRepository",
    "SessionCheckpointStore",
    "_deserialize_session",
    "_serialize_session",
]
