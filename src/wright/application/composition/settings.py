"""Bind the settings service to the JSON store."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ...core.logger import set_configured_terminal_level
from ...core.paths import skill_directories, user_skills_dir
from ...domain.model.settings import SettingsError, WrightSettings
from ...infrastructure.config.mcp_config_store import JsonMcpConfigStore
from ...infrastructure.config.permission_store import (
    FilePermissionRepository,
    default_settings_path,
)
from ...infrastructure.config.secret_store import DotenvSecretStore
from ...infrastructure.config.settings_store import JsonSettingsStore
from ...infrastructure.storage.skill_registry import SkillRegistry
from ..permission.defaults import PermissionDefaults
from ..settings import SettingsService
from ..settings.secrets import SecretsService
from ..skills.catalog import EnabledSkills
from ..workspace.mcp_config import McpConfigService


def settings_service(project_root: Path | None) -> SettingsService:
    return SettingsService(JsonSettingsStore(project_root))


def secrets_service() -> SecretsService:
    return SecretsService(DotenvSecretStore())


def permission_defaults(project_root: Path | None) -> PermissionDefaults:
    return PermissionDefaults(FilePermissionRepository(project_root), settings_path=str(default_settings_path()))


def mcp_config_service(project_root: Path | None) -> McpConfigService:
    """Project servers count as trusted only through the user-only mcp.trust_project."""
    trusted = settings_service(None).load().mcp.trust_project
    return McpConfigService(JsonMcpConfigStore(project_root), project_trusted=trusted)


def skill_switches(project_root: Path | None) -> EnabledSkills:
    """The skill catalog with its on/off switches, without a session."""
    directories = skill_directories(project_root) if project_root is not None else [user_skills_dir()]
    return EnabledSkills(SkillRegistry(directories), disabled_skills(project_root), write_disabled_skills(project_root))


def load_settings(project_root: Path | None) -> WrightSettings:
    """Effective settings for a project. Raises SettingsError on an unusable user file."""
    settings = settings_service(project_root).load()
    # The logger exists before settings do and reads the environment per
    # record; it only needs the level when it came from the settings file.
    source = settings.sources.get("logging.level")
    set_configured_terminal_level(settings.logging.level if source == "user" else None)
    return settings


def sandbox_egress() -> str:
    """``sandbox.egress`` from the user layer, read when a command starts.

    An unreadable settings file gives ``proxy``, the stricter mode.
    """
    try:
        return settings_service(None).load().sandbox.egress
    except SettingsError:
        return "proxy"


def disabled_skills(project_root: Path | None, initial: tuple[str, ...] = ()) -> Callable[[], tuple[str, ...]]:
    """Read ``skills.disabled`` afresh on each call.

    A settings file that stops parsing keeps the last good value, so a typo
    does not quietly turn every skill back on.
    """
    last = [initial]

    def read() -> tuple[str, ...]:
        try:
            last[0] = settings_service(project_root).load().skills.disabled
        except SettingsError:
            pass
        return last[0]

    return read


def write_disabled_skills(project_root: Path | None) -> Callable[[tuple[str, ...]], None]:
    """Save ``skills.disabled`` to the project file, or the user file without a project."""

    def write(ids: tuple[str, ...]) -> None:
        service = settings_service(project_root)
        if service.load().sources.get("skills.disabled") == "env":
            raise SettingsError("skills.disabled is set by WRIGHT_DISABLED_SKILLS; change it there")
        service.update("project" if project_root is not None else "user", "skills.disabled", list(ids))

    return write


__all__ = [
    "disabled_skills",
    "load_settings",
    "mcp_config_service",
    "permission_defaults",
    "sandbox_egress",
    "secrets_service",
    "settings_service",
    "skill_switches",
    "write_disabled_skills",
]
