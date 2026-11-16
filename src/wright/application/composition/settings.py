"""Bind the settings service to the JSON store."""

from __future__ import annotations

from pathlib import Path

from ...core.logger import set_configured_terminal_level
from ...domain.model.settings import WrightSettings
from ...infrastructure.config.settings_store import JsonSettingsStore
from ..settings import SettingsService


def settings_service(project_root: Path | None) -> SettingsService:
    return SettingsService(JsonSettingsStore(project_root))


def load_settings(project_root: Path | None) -> WrightSettings:
    """Effective settings for a project. Raises SettingsError on an unusable user file."""
    settings = settings_service(project_root).load()
    # The logger exists before settings do and reads the environment per
    # record; it only needs the level when it came from the settings file.
    source = settings.sources.get("logging.level")
    set_configured_terminal_level(settings.logging.level if source == "user" else None)
    return settings


__all__ = ["load_settings", "settings_service"]
