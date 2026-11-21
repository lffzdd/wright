"""Where API keys are kept: Wright's own ``.env`` and the process environment."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


class SecretStore(Protocol):
    """The user's key file plus the live environment.

    Nothing here returns a stored value to a caller outside the store: the
    service only needs to know whether a key is set and where.
    """

    def path(self) -> Path: ...

    def stored(self) -> Mapping[str, str]:
        """Non-empty values in the key file, for comparison only."""
        ...

    def environment(self) -> Mapping[str, str]: ...

    def write(self, name: str, value: str | None) -> None:
        """Set one key in the file and the environment; ``None`` removes it."""
        ...


__all__ = ["SecretStore"]
