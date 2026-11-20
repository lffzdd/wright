"""Resolve and edit typed settings for one project."""

from __future__ import annotations

from typing import Any

from ...core.logger import get_logger
from ...domain.gateway.settings import SettingsStore
from ...domain.model.settings import (
    SETTING_SPECS,
    SettingScope,
    SettingsError,
    WrightSettings,
)
from ...domain.policy.settings import (
    ResolvedSettings,
    environment_layer,
    flatten,
    resolve_settings,
    validate_write,
)
from .models import model_overview

logger = get_logger(__name__)


class SettingsService:
    def __init__(self, store: SettingsStore) -> None:
        self.store = store

    def resolve(self) -> ResolvedSettings:
        """Merge every layer. Raises SettingsError when a trusted layer is unusable.

        An unreadable user file is an error, because ignoring it could drop
        an endpoint the person set. An unreadable project file is only a
        warning: it can set nothing that matters for safety.
        """
        user = self.store.read("user")
        warnings: list[str] = []
        try:
            project = self.store.read("project")
        except SettingsError as exc:
            warnings.append(f"Ignored project settings: {exc}")
            project = {}
        resolved = resolve_settings(
            user=user,
            project=project,
            environ=self.store.environment(),
            user_label=str(self.store.path("user")),
            project_label=str(self.store.path("project") or "project settings"),
        )
        if warnings:
            resolved = ResolvedSettings(resolved.settings, (*warnings, *resolved.warnings))
        return resolved

    def load(self) -> WrightSettings:
        """Resolve and log any ignored keys."""
        resolved = self.resolve()
        for warning in resolved.warnings:
            logger.warning("%s", warning)
        return resolved.settings

    def describe(self) -> dict[str, Any]:
        """Every key with its effective value and layer, for a settings editor."""
        resolved = self.resolve()
        settings = resolved.settings
        env_keys = environment_layer(self.store.environment())
        user = flatten(self.store.read("user"))
        try:
            project = flatten(self.store.read("project"))
        except SettingsError:
            project = {}
        project_path = self.store.path("project")
        items = []
        for spec in SETTING_SPECS:
            items.append({
                "key": spec.key,
                "kind": spec.kind,
                "choices": list(spec.choices),
                "minimum": spec.minimum,
                "description": spec.description,
                "default": spec.to_json(spec.default),
                "value": spec.to_json(settings.value(spec.key)),
                "source": settings.sources.get(spec.key, "default"),
                "user_value": user.get(spec.key),
                "project_value": project.get(spec.key),
                "env": list(spec.env),
                "env_override": spec.key in env_keys,
                "scopes": ["user", "project"] if spec.project_safe else ["user"],
            })
        return {
            "paths": {
                "user": str(self.store.path("user")),
                "project": str(project_path) if project_path is not None else None,
            },
            "settings": items,
            "warnings": list(resolved.warnings),
            "models": model_overview(settings.llm, self.store.environment()),
        }

    def update(self, scope: SettingScope, key: str, value: Any) -> None:
        """Write one key to the user or project file; ``None`` removes it."""
        if scope == "project" and self.store.path("project") is None:
            raise SettingsError("No project is open, so there is no project settings file")
        self.store.write(scope, key, validate_write(scope, key, value))


__all__ = ["SettingsService"]
