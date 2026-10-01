"""List and revoke session or persistent allow rules.

Revocation changes the stored rule set used by the next resolution. It does
not rewrite an invocation grant that was already issued.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ...domain.policy.permission.resolver import PermissionPolicy
from ...domain.policy.permission.scope import AccessScope, resolve_root
from ...domain.policy.permission.settings import PermissionRule
from ...infrastructure.config.permission_store import (
    append_additional_directory,
    remove_additional_directory,
    remove_allow_rule,
)


class GrantError(ValueError):
    pass


def list_grants(session: Any, settings: Any) -> dict[str, Any]:
    session_rules = []
    for rule in list(getattr(session, "permission_rules", []) or []):
        session_rules.append({
            "id": _rule_id(rule),
            "scope": "session",
            "persistence": "session",
            "rule": rule,
        })
    persistent = []
    allow = list(getattr(settings, "allow", []) or [])
    for rule in allow:
        payload = rule.to_persistent() if hasattr(rule, "to_persistent") else rule
        persistent.append({
            "id": _rule_id(payload if isinstance(payload, dict) else {"text": str(payload)}),
            "scope": "persistent",
            "persistence": "user",
            "rule": payload if isinstance(payload, dict) else {"text": str(payload)},
        })
    directories = [str(path) for path in getattr(session, "additional_working_directories", []) or []]
    extra = list(getattr(settings, "additional_directories", []) or [])
    root = getattr(session, "workspace_dir", None)
    roots = [str(path) for path in AccessScope(
        resolve_root(root), tuple(resolve_root(path) for path in directories),
    ).roots] if root else []
    parsed_rules = tuple(
        PermissionRule.from_persistent(item["rule"]) for item in session_rules
    )
    return {
        "permission_mode": getattr(session, "permission_mode", None) or getattr(settings, "mode", "default"),
        "interaction_mode": getattr(session, "interaction_mode", "agent"),
        "session_rules": session_rules,
        "persistent_rules": persistent,
        "session_directories": directories,
        "persistent_directories": extra,
        "effective_policy": PermissionPolicy(settings).summarize(
            roots=roots, session_rules=parsed_rules,
            interaction_mode=getattr(session, "interaction_mode", "agent"),
            permission_mode=getattr(session, "permission_mode", None),
        ),
        "boundaries": [
            "deny rules still win",
            "protected permission files stay blocked",
            "ask and plan ceilings are not widened by bypass",
            "full access is the existing bypass mode, not an unconditional override",
        ],
    }


def revoke_session_rule(session: Any, rule_id: str, *, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise GrantError("confirmation is required")
    for rule in list(getattr(session, "permission_rules", []) or []):
        if _rule_id(rule) == rule_id:
            removed = session.remove_permission_rule(rule)
            return {"ok": removed, "id": rule_id, "scope": "session"}
    raise GrantError("session grant was not found")


def revoke_persistent_rule(settings: Any, rule_id: str, *, confirm: bool, path: Any = None) -> dict[str, Any]:
    if not confirm:
        raise GrantError("confirmation is required")
    for rule in list(getattr(settings, "allow", []) or []):
        payload = rule.to_persistent() if hasattr(rule, "to_persistent") else None
        if not isinstance(payload, dict):
            continue
        if _rule_id(payload) != rule_id:
            continue
        remove_allow_rule(payload, path)
        settings.allow = [
            item for item in settings.allow
            if not (hasattr(item, "to_persistent") and _rule_id(item.to_persistent()) == rule_id)
        ]
        return {"ok": True, "id": rule_id, "scope": "persistent"}
    raise GrantError("persistent grant was not found")


def change_directory(
    session: Any,
    settings: Any,
    path: str,
    *,
    scope: str,
    confirm: bool,
    action: str,
    store_path: Any = None,
) -> dict[str, Any]:
    """Add or remove an extra working directory.

    The session execution root is not an extra directory and cannot be removed
    here. A persistent change is written through the permission settings file.
    """

    if not confirm:
        raise GrantError("confirmation is required")
    if scope not in {"session", "persistent"}:
        raise GrantError("scope must be session or persistent")
    if action not in {"add", "remove"}:
        raise GrantError("action must be add or remove")
    try:
        resolved = resolve_root(path)
    except (OSError, ValueError) as exc:
        raise GrantError(str(exc)) from exc
    if not resolved.is_dir():
        raise GrantError("directory does not exist")
    origin = getattr(session, "workspace_dir", None)
    if origin is not None and resolve_root(origin) == resolved and action == "remove":
        raise GrantError("the execution root cannot be removed")
    text = str(resolved)
    if scope == "session":
        if action == "add":
            session.add_working_directory(resolved)
        elif not session.remove_working_directory(resolved):
            raise GrantError("session directory grant was not found")
    else:
        if action == "add":
            append_additional_directory(text, store_path)
            current = list(getattr(settings, "additional_directories", []) or [])
            if text not in current:
                current.append(text)
            settings.additional_directories = current
        else:
            current = list(getattr(settings, "additional_directories", []) or [])
            if text not in current:
                raise GrantError("persistent directory grant was not found")
            remove_additional_directory(text, store_path)
            settings.additional_directories = [item for item in current if item != text]
    return {"ok": True, "scope": scope, "action": action, "path": text}


def add_session_rule(session: Any, rule: dict[str, Any]) -> dict[str, Any]:
    parsed = PermissionRule.from_persistent(rule)
    payload = parsed.to_persistent()
    if not isinstance(payload, dict):
        raise GrantError("rule could not be stored")
    session.add_permission_rule(payload)
    return {"id": _rule_id(payload), "scope": "session", "rule": payload}


def _rule_id(rule: object) -> str:
    encoded = json.dumps(rule, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "GrantError",
    "add_session_rule",
    "change_directory",
    "list_grants",
    "revoke_persistent_rule",
    "revoke_session_rule",
]
