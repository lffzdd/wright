"""Where settings layers are read from and written to."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from ..model.settings import SettingScope


class SettingsStore(Protocol):
    """Settings files for one project, plus the process environment.

    ``read`` returns the nested file contents, empty when the file is
    missing, and raises ``SettingsError`` when it cannot be parsed.
    """

    def path(self, scope: SettingScope) -> Path | None: ...

    def read(self, scope: SettingScope) -> Mapping[str, Any]: ...

    def write(self, scope: SettingScope, key: str, value: Any) -> None:
        """Set one dotted key; ``None`` removes it."""
        ...

    def environment(self) -> Mapping[str, str]: ...


__all__ = ["SettingsStore"]
