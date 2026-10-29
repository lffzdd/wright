"""Readable projections and explicit resource grants from the permission service."""

from __future__ import annotations

from typing import Any

from ...domain.policy.permission.resolver import PermissionPolicy
from ..permission.projection import summarize_policy
from ..permission.service import PermissionService, record_id


def list_grants(session: Any, permissions: PermissionService, sandbox: dict | None = None) -> dict[str, Any]:
    snapshot = permissions.snapshot(session)
    rows = []
    for rule in (*snapshot.rules, *snapshot.settings.allow):
        target = rule.display_target(str(snapshot.scope.origin))
        rows.append({"id": record_id(rule.to_persistent()), "lifetime": rule.lifetime, "source": rule.lifetime,
                     "resource_kind": rule.kind, "target": target, "operations": list(rule.operations or (("shell",) if rule.kind in {"shell", "command"} else ("network_read",) if rule.kind == "network" else ())),
                     "recursive": rule.recursive, "tool": rule.tool_name, "cwd": rule.cwd,
                     "project_id": rule.project_id, "description": rule.describe(), "version": snapshot.version,
                     "resource": rule.to_persistent(), "http_methods": list(rule.methods), "protocol": rule.scheme, "host": rule.host, "port": rule.port})
    return {"version": snapshot.version, "grants": rows,
            "permission_mode": snapshot.mode,
            "execution_mode": session.execution_mode,
            "interaction_mode": snapshot.interaction_mode,
            "sandbox": sandbox, "permission_data_version": 2,
            "effective_policy": summarize_policy(PermissionPolicy(snapshot.settings),
                roots=[str(path) for path in snapshot.scope.roots], read_only=tuple(str(path) for path in snapshot.scope.read_only), session_rules=snapshot.rules,
                interaction_mode=snapshot.interaction_mode, permission_mode=snapshot.mode, execution_mode=session.execution_mode),
            "boundary_codes": ["web.boundary.protected", "web.boundary.priority", "web.boundary.shell" if session.execution_mode == "sandbox" else "web.boundary.host_shell"]}
