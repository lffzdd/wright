"""Checkpoint failures shared by the codec, link checks, and repository."""

from __future__ import annotations


class CheckpointError(ValueError):
    """Checkpoint data is missing, corrupt, unsupported, or inconsistent."""
