"""What new sessions start with: the default permission mode and the stored rules.

The Settings page reads and edits these without a session. Edits only touch
the user and project files through the repository, which validates the whole
file before and after each change and refuses anything that does not parse.
Rules can be added only as deny or ask: allow rules come from approvals,
which bound them to a resource. Removing a rule never grants access; the
mode decides again. Built-in deny rules stay locked.
"""

from __future__ import annotations

from typing import Any, get_args

from ...domain.gateway.permissions import PermissionRepository
from ...domain.policy.permission.settings import PermissionMode, PermissionRule

PERMISSION_MODES: tuple[str, ...] = get_args(PermissionMode)
_BUILTIN = "builtin"


class PermissionDefaultsError(ValueError):
    """An edit that the permission files cannot take."""


class PermissionDefaults:
    def __init__(self, repository: PermissionRepository, *, settings_path: str) -> None:
        self.repository = repository
        self.settings_path = settings_path

    @property
    def has_project(self) -> bool:
        return bool(self.repository.project_identity)

    def describe(self) -> dict[str, Any]:
        try:
            settings, _ = self.repository.read()
        except (ValueError, TypeError, OSError) as exc:
            raise PermissionDefaultsError(f"Could not read permission settings: {exc}") from exc
        rules = [
            *(_row(rule, "allow") for rule in settings.allow),
            *(_row(rule, "ask") for rule in settings.ask),
            *(_row(rule, "deny") for rule in settings.deny),
        ]
        return {
            "mode": settings.mode,
            "modes": list(PERMISSION_MODES),
            "path": self.settings_path,
            "project": self.has_project,
            "rules": rules,
        }

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in PERMISSION_MODES:
            raise PermissionDefaultsError(f"Unknown permission mode {mode!r}; choose one of {', '.join(PERMISSION_MODES)}")
        self._apply(lambda: self.repository.set_default_mode(mode))
        return self.describe()

    def add_rule(self, text: str, effect: str, scope: str) -> dict[str, Any]:
        text = text.strip()
        if effect not in {"deny", "ask"}:
            raise PermissionDefaultsError("Only deny and ask rules can be added; allow rules come from approvals")
        if not text or "\n" in text:
            raise PermissionDefaultsError("Write one rule, such as shell(rm -rf *) or read(**/.env)")
        self._check_scope(scope)
        self._apply(lambda: self.repository.add_restriction(text, effect, scope))
        return self.describe()

    def remove_rule(self, rule_id: str, scope: str) -> dict[str, Any]:
        self._check_scope(scope)
        current = self.describe()["rules"]
        match = next((row for row in current if row["id"] == rule_id and row["scope"] == scope), None)
        if match is None:
            raise PermissionDefaultsError("That rule is no longer there")
        if match["locked"]:
            raise PermissionDefaultsError("Built-in deny rules cannot be removed")
        self._apply(lambda: self.repository.remove(rule_id, scope))
        return self.describe()

    def _check_scope(self, scope: str) -> None:
        if scope not in {"user", "project"}:
            raise PermissionDefaultsError("Scope must be user or project")
        if scope == "project" and not self.has_project:
            raise PermissionDefaultsError("No project is open")

    @staticmethod
    def _apply(change) -> None:
        try:
            change()
        except (ValueError, TypeError) as exc:
            # The file is left as it was: the repository validates before writing.
            raise PermissionDefaultsError(str(exc)) from exc


def _row(rule: PermissionRule, effect: str) -> dict[str, Any]:
    return {
        "id": rule.id,
        "effect": effect,
        "scope": rule.lifetime,
        "text": _text(rule),
        "description": rule.describe(),
        "locked": rule.source == _BUILTIN,
    }


def _text(rule: PermissionRule) -> str:
    if rule.legacy_text:
        return rule.legacy_text
    if rule.is_host_rule:
        return f"network({rule.host_text()})"
    if rule.kind == "shell":
        return f"{rule.tool_name}({rule.subject_glob or rule.pattern})"
    if rule.kind == "tool":
        return rule.tool_name
    if rule.kind in {"file", "directory"}:
        return f"{rule.tool_name}({rule.subject_glob or rule.root or rule.pattern})"
    return rule.describe()


__all__ = ["PERMISSION_MODES", "PermissionDefaults", "PermissionDefaultsError"]
