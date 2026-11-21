"""Set and remove the API keys Wright knows, without ever reading one back.

Only named keys may be written, and only into the user's ``~/.wright/.env``.
A key set outside that file (the shell, or a project ``.env``) wins over it
at startup, so the page shows it as set elsewhere and leaves it alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ...domain.gateway.secrets import SecretStore
from ...domain.model.llm.catalog import provider_key_envs
from ...domain.model.settings import SettingsError

SecretSource = Literal["file", "env", "missing"]
SecretGroup = Literal["models", "search", "knowledge"]

_MAX_LENGTH = 4096


@dataclass(frozen=True)
class ManagedSecret:
    name: str
    group: SecretGroup


def managed_secrets() -> tuple[ManagedSecret, ...]:
    return (
        *(ManagedSecret(name, "models") for name in provider_key_envs()),
        ManagedSecret("TAVILY_API_KEY", "search"),
        ManagedSecret("BRAVE_API_KEY", "search"),
        ManagedSecret("SILICONFLOW_API_KEY", "knowledge"),
    )


class SecretsService:
    def __init__(self, store: SecretStore) -> None:
        self.store = store

    def source(self, name: str) -> SecretSource:
        stored = self.store.stored().get(name)
        live = (self.store.environment().get(name) or "").strip()
        if stored and (not live or live == stored):
            return "file"
        return "env" if live else "missing"

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.store.path()),
            "secrets": [
                {"name": item.name, "group": item.group, "source": self.source(item.name)}
                for item in managed_secrets()
            ],
        }

    def set(self, name: str, value: str | None) -> dict[str, Any]:
        """Write one key; ``None`` or an empty value removes it."""
        self._check(name)
        text = (value or "").strip()
        if text and (len(text) > _MAX_LENGTH or any(ch in text for ch in "'\"\\") or not text.isprintable()):
            raise SettingsError(f"{name} must be one line of printable text without quotes or backslashes")
        self.store.write(name, text or None)
        return self.describe()

    def _check(self, name: str) -> None:
        if name not in {item.name for item in managed_secrets()}:
            raise SettingsError(f"{name} is not a key Wright manages")
        if self.source(name) == "env":
            raise SettingsError(f"{name} is set outside {self.store.path()}; change or unset it there")


__all__ = ["ManagedSecret", "SecretsService", "managed_secrets"]
