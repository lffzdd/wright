"""HTTP adapter for typed runtime settings (see docs/settings.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel

from ...application.composition.settings import settings_service
from ...domain.model.settings import SettingsError


class SettingUpdateRequest(BaseModel):
    scope: Literal["user", "project"]
    key: str
    # null removes the key from that file.
    value: Any = None


def describe_settings(project_root: Path | None) -> dict[str, Any]:
    try:
        return settings_service(project_root).describe()
    except SettingsError as exc:
        # An unreadable user file: say so instead of showing defaults.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def update_setting(project_root: Path | None, body: SettingUpdateRequest) -> dict[str, Any]:
    service = settings_service(project_root)
    try:
        service.update(body.scope, body.key, body.value)
    except SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return describe_settings(project_root)


__all__ = ["SettingUpdateRequest", "describe_settings", "update_setting"]
