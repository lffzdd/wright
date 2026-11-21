"""HTTP adapter for the Settings page (see docs/settings.md).

Every project-aware route exists twice: under ``/api/v1`` for the user layer
only, and under ``/api/v1/workspaces/{id}`` with that project's files too.
API keys and hooks belong to the user alone, so they have only the first.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...application.composition.lifecycle_loader import describe_hooks
from ...application.composition.settings import (
    load_settings,
    mcp_config_service,
    permission_defaults,
    secrets_service,
    settings_service,
    skill_switches,
)
from ...application.permission.defaults import PermissionDefaultsError
from ...application.workspace.mcp_config import McpConfigError
from ...domain.model.settings import SettingsError
from ...domain.model.skills import SkillNotFoundError

Scope = Literal["user", "project"]


class SettingUpdateRequest(BaseModel):
    scope: Scope
    key: str
    # null removes the key from that file.
    value: Any = None


class SecretRequest(BaseModel):
    # null or empty removes the key.
    value: str | None = None


class ModeRequest(BaseModel):
    mode: str


class RuleRequest(BaseModel):
    effect: Literal["deny", "ask"]
    scope: Scope
    text: str


class SkillRequest(BaseModel):
    enabled: bool


class McpServerRequest(BaseModel):
    scope: Scope
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    # A null value keeps the stored one, so the page never needs to see it.
    env: dict[str, str | None] | None = None
    headers: dict[str, str | None] | None = None
    # The server's name before a rename.
    previous: str | None = None


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


def _refused(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


def add_settings_routes(
    router: APIRouter,
    require: Callable[[Request], None],
    resolve_root: Callable[[str], Path],
) -> None:
    def no_project() -> Path | None:
        return None

    def workspace(registered_id: str) -> Path | None:
        return resolve_root(registered_id)

    for prefix, root_of in (("", no_project), ("/workspaces/{registered_id}", workspace)):
        _add_project_routes(router, prefix, Depends(require), Depends(root_of))
    _add_user_routes(router, Depends(require))


def _add_project_routes(router: APIRouter, prefix: str, auth: Any, root_dependency: Any) -> None:
    base = f"{prefix}/settings"

    @router.get(base)
    def read_settings(_: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        return describe_settings(root)

    @router.put(base)
    def write_setting(body: SettingUpdateRequest, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        return update_setting(root, body)

    @router.get(f"{base}/permissions")
    def read_permissions(_: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        try:
            return permission_defaults(root).describe()
        except PermissionDefaultsError as exc:
            raise _refused(exc, 409) from exc

    @router.put(f"{base}/permissions/mode")
    def write_mode(body: ModeRequest, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        try:
            return permission_defaults(root).set_mode(body.mode)
        except PermissionDefaultsError as exc:
            raise _refused(exc) from exc

    @router.post(f"{base}/permissions/rules")
    def add_rule(body: RuleRequest, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        try:
            return permission_defaults(root).add_rule(body.text, body.effect, body.scope)
        except PermissionDefaultsError as exc:
            raise _refused(exc) from exc

    @router.delete(f"{base}/permissions/rules/{{scope}}/{{rule_id}}")
    def remove_rule(scope: Scope, rule_id: str, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        try:
            return permission_defaults(root).remove_rule(rule_id, scope)
        except PermissionDefaultsError as exc:
            raise _refused(exc) from exc

    @router.get(f"{base}/skills")
    def read_skills(_: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        return _skills_view(root)

    @router.put(f"{base}/skills/{{skill_id}}")
    def write_skill(skill_id: str, body: SkillRequest, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        try:
            skill_switches(root).set_enabled(skill_id, body.enabled)
        except SkillNotFoundError as exc:
            raise _refused(exc, 404) from exc
        except SettingsError as exc:
            raise _refused(exc) from exc
        return _skills_view(root)

    @router.get(f"{base}/mcp")
    def read_mcp(_: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        return _mcp(root).describe()

    @router.put(f"{base}/mcp/{{name}}")
    def write_mcp(name: str, body: McpServerRequest, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        form = body.model_dump(exclude={"scope", "previous"})
        try:
            return _mcp(root).save(body.scope, name, form, previous=body.previous)
        except McpConfigError as exc:
            raise _refused(exc) from exc

    @router.delete(f"{base}/mcp/{{scope}}/{{name}}")
    def remove_mcp(scope: Scope, name: str, _: None = auth, root: Path | None = root_dependency) -> dict[str, Any]:
        try:
            return _mcp(root).remove(scope, name)
        except McpConfigError as exc:
            raise _refused(exc) from exc


def _add_user_routes(router: APIRouter, auth: Any) -> None:
    @router.get("/settings/secrets")
    def read_secrets(_: None = auth) -> dict[str, Any]:
        return secrets_service().describe()

    @router.put("/settings/secrets/{name}")
    def write_secret(name: str, body: SecretRequest, _: None = auth) -> dict[str, Any]:
        try:
            return secrets_service().set(name, body.value)
        except SettingsError as exc:
            raise _refused(exc) from exc

    @router.delete("/settings/secrets/{name}")
    def remove_secret(name: str, _: None = auth) -> dict[str, Any]:
        try:
            return secrets_service().set(name, None)
        except SettingsError as exc:
            raise _refused(exc) from exc

    @router.get("/settings/hooks")
    def read_hooks(_: None = auth) -> dict[str, Any]:
        try:
            path = load_settings(None).hooks.config
        except SettingsError as exc:
            raise _refused(exc, 409) from exc
        return describe_hooks(path)


def _skills_view(root: Path | None) -> dict[str, Any]:
    try:
        source = settings_service(root).load().sources.get("skills.disabled", "default")
    except SettingsError as exc:
        raise _refused(exc, 409) from exc
    catalog = skill_switches(root)
    return {"skills": catalog.rows(), "errors": list(catalog.scan_errors()), "locked": source == "env"}


def _mcp(root: Path | None):
    try:
        return mcp_config_service(root)
    except SettingsError as exc:
        raise _refused(exc, 409) from exc


__all__ = ["SettingUpdateRequest", "add_settings_routes", "describe_settings", "update_setting"]
