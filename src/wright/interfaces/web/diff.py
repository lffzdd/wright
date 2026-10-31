"""Web imports for the shared workspace diff use case."""

from ...application.workspace.changes import (
    MAX_PATCH_BYTES,
    DiffError,
    change_patch,
    list_changes,
)

__all__ = ["MAX_PATCH_BYTES", "DiffError", "change_patch", "list_changes"]
