"""Readable projections and explicit resource grants from the permission service."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from ...domain.policy.permission.resolver import PermissionPolicy
from ...domain.policy.permission.settings import PermissionRule, relative_to_root
from ...domain.policy.permission.types import AuthorizationChange
from ..tool_execution.permissions import PermissionService, record_id


class GrantError(ValueError):
    pass


def list_grants(session: Any, permissions: PermissionService, sandbox: dict | None = None) -> dict[str, Any]:
    snapshot = permissions.snapshot(session)
    rows = []
    for rule in (*snapshot.rules, *snapshot.settings.allow):
        target = rule.display_target(str(snapshot.scope.origin))
        rows.append({"id": record_id(rule.to_persistent()), "lifetime": rule.lifetime, "source": rule.lifetime,
                     "resource_kind": rule.kind, "target": target, "operations": list(rule.operations or (("shell",) if rule.kind == "shell" else ("network_read",) if rule.kind == "network" else ())),
                     "recursive": rule.recursive, "tool": rule.tool_name, "cwd": rule.cwd,
                     "project_id": rule.project_id, "description": rule.describe(), "version": snapshot.version,
                     "http_methods": list(rule.methods), "protocol": rule.scheme, "host": rule.host, "port": rule.port})
    return {"version": snapshot.version, "grants": rows,
            "permission_mode": snapshot.mode,
            "interaction_mode": snapshot.interaction_mode,
            "sandbox": sandbox, "permission_data_version": 2,
            "effective_policy": PermissionPolicy(snapshot.settings).summarize(
                roots=[str(path) for path in snapshot.scope.roots], read_only=tuple(str(path) for path in snapshot.scope.read_only), session_rules=snapshot.rules,
                interaction_mode=snapshot.interaction_mode, permission_mode=snapshot.mode),
            "boundary_codes": ["web.boundary.protected", "web.boundary.priority", "web.boundary.shell"]}


def resource_change(session, backend, snapshot, resource: dict, lifetime: str) -> AuthorizationChange:
    """Normalize with the backend; reject raw tool and wildcard grants from UI."""
    kind, path = resource.get("kind"), resource.get("path")
    if kind not in {"file", "directory"} or not isinstance(path, str) or not path.strip():
        raise GrantError("Choose a file or directory and enter its path")
    if lifetime not in {"session", "project", "user"}:
        raise GrantError("Choose a supported authorization lifetime")
    operations = resource.get("operations")
    if operations not in (["file_read"], ["file_read", "file_write"]):
        raise GrantError("Choose read-only or read and write access")
    resolved = backend.resolve_path(path)
    if snapshot.scope.classify(resolved.value).value == "forbidden":
        raise GrantError("Protected permission resources cannot be authorized")
    root = resolved.value if kind == "directory" else resolved.parent.value
    pattern = "" if kind == "directory" else os.path.basename(resolved.value)
    rule = PermissionRule("*", kind=kind, root=root, pattern=pattern, operations=tuple(operations),
                          exact=kind == "file", recursive=kind == "directory")
    relative = relative_to_root(resolved.value, str(session.workspace_dir))
    if lifetime in {"session", "project"} and (relative is not None or resolved.value == str(session.workspace_dir)):
        rule = replace(rule, root="", root_kind="workspace", pattern=relative or "")
    record = rule.to_persistent()
    return AuthorizationChange(session_rules=(record,) if lifetime == "session" else (),
                               project_rules=(record,) if lifetime == "project" else (),
                               persistent_rules=(record,) if lifetime == "user" else ())
