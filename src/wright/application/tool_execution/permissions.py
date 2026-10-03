"""Application authorization lifecycle shared by interactive and durable calls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ...domain.gateway.permissions import PermissionRepository
from ...domain.policy.permission.scope import AccessScope
from ...domain.policy.permission.settings import PermissionRule, PermissionSettings
from ...domain.policy.permission.types import AuthorizationChange


class PermissionConflict(ValueError):
    pass


@dataclass(frozen=True)
class PermissionSnapshot:
    settings: PermissionSettings
    rules: tuple[PermissionRule, ...]
    scope: AccessScope
    version: str
    mode: str
    interaction_mode: str


def record_id(record: dict) -> str:
    return str(record.get("id") or hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24])


def session_owner(session):
    """Interactive descendants share the user's resumable conversation."""
    seen = {id(session)}
    while (parent := getattr(session, "permission_parent", None)) is not None and id(parent) not in seen:
        seen.add(id(parent))
        session = parent
    return session


class PermissionService:
    def __init__(self, repository: PermissionRepository, *, execution_factory=None):
        self.repository = repository
        self.execution_factory = execution_factory
        self.inline_settings = None

    def snapshot(self, session) -> PermissionSnapshot:
        with self.repository.transaction():
            return self._snapshot(session)

    def _snapshot(self, session) -> PermissionSnapshot:
        settings, protected = self.repository.read()
        if self.inline_settings is not None:
            settings.mode = self.inline_settings.mode
            for name in ("allow", "deny", "ask"):
                getattr(settings, name).extend(getattr(self.inline_settings, name))
        records_list = []
        session_versions = []
        modes = []
        owner = session
        visited = set()
        while owner is not None and id(owner) not in visited:
            visited.add(id(owner))
            latest, revision = self.repository.read_session(owner.session_id, tuple(owner.permission_rules))
            owner.permission_rules = latest
            session_versions.append(revision)
            modes.append((owner.permission_mode, owner.interaction_mode))
            records_list.extend(PermissionRule.from_mapping(rule) for rule in latest)
            owner = getattr(owner, "permission_parent", None)
        records = tuple(records_list)
        mode = next((value for value, _ in reversed(modes) if value), settings.mode)
        if any(value == "plan" for value, _ in modes):
            mode = "plan"
        interaction = "ask" if any(value == "ask" for _, value in modes) else "plan" if any(value == "plan" for _, value in modes) else session.interaction_mode
        directories = [Path(rule.resource_root(str(session.workspace_dir))) for rule in (*settings.allow, *records)
                       if rule.kind == "directory"]
        read_only = tuple(Path(rule.resource_root(str(session.workspace_dir))) for rule in (*settings.allow, *records)
                          if rule.kind == "directory" and "file_write" not in rule.operations)
        scope = AccessScope(session.workspace_dir, tuple(directories), tuple(Path(path) for path in protected), session.project_root or session.workspace_dir, read_only)
        payload = {"session_revisions": session_versions, "revision": settings.revision, "settings": {key: [rule.to_persistent() for rule in getattr(settings, key)] for key in ("allow", "deny", "ask")},
                   "session": [rule.to_persistent() for rule in records], "mode": mode, "ancestor_modes": modes,
                   "interaction": interaction, "workspace": str(session.workspace_dir)}
        version = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return PermissionSnapshot(settings, records, scope, version, mode, interaction)

    def validate(self, session, version: str) -> None:
        if self.snapshot(session).version != version:
            raise PermissionConflict("Permissions changed; request approval again")

    def commit(self, change: AuthorizationChange, session, save_checkpoint=None, *, expected_version: str | None = None) -> None:
        with self.repository.transaction():
            if expected_version is not None:
                self.validate(session, expected_version)
            self.snapshot(session)
            self._commit(change, session, save_checkpoint)

    def _commit(self, change: AuthorizationChange, session, save_checkpoint=None) -> None:
        saved = []
        owner = session_owner(session)
        before = [dict(record) for record in owner.permission_rules]
        try:
            for lifetime, records in (("session", change.session_rules), ("project", change.project_rules), ("user", change.persistent_rules)):
                stamped = tuple({**record, "id": uuid4().hex, "lifetime": lifetime, "project_id": str(getattr(self.repository, "project_identity", "")) if lifetime != "user" else ""} for record in records)
                if lifetime == "session":
                    for record in stamped:
                        owner.add_permission_rule(record)
                elif stamped:
                    self.repository.add(stamped, lifetime)
                    saved.extend((record["id"], lifetime) for record in stamped)
            if change.session_rules:
                self.repository.write_session(owner.session_id, tuple(owner.permission_rules))
            if save_checkpoint is not None:
                save_checkpoint(session)
        except Exception:
            owner.permission_rules = before
            self.repository.write_session(owner.session_id, tuple(before))
            for identity, lifetime in saved:
                self.repository.remove(identity, lifetime)
            raise

    def revoke(self, session, identity: str, lifetime: str, version: str, save_checkpoint=None) -> None:
        with self.repository.transaction():
            snapshot = self.snapshot(session)
            owner = session_owner(session)
            before = [dict(item) for item in owner.permission_rules]
            removed = tuple(rule.to_persistent() for rule in snapshot.settings.allow if rule.lifetime == lifetime and rule.id == identity)
            self._revoke(session, identity, lifetime, version)
            if lifetime == "session":
                self.repository.write_session(owner.session_id, tuple(owner.permission_rules))
            try:
                if save_checkpoint is not None:
                    save_checkpoint(session)
            except Exception:
                owner.permission_rules = before
                self.repository.write_session(owner.session_id, tuple(before))
                if removed:
                    self.repository.add(removed, lifetime)
                raise

    def _revoke(self, session, identity: str, lifetime: str, version: str) -> None:
        self.validate(session, version)
        if lifetime == "session":
            owner = session_owner(session)
            matches = [item for item in owner.permission_rules if record_id(item) == identity]
            if not matches:
                raise ValueError("Authorization not found")
            owner.permission_rules = [item for item in owner.permission_rules if record_id(item) != identity]
        elif lifetime in {"project", "user"}:
            snapshot = self.snapshot(session)
            if not any(rule.lifetime == lifetime and record_id(rule.to_persistent()) == identity for rule in snapshot.settings.allow):
                raise ValueError("Authorization not found")
            self.repository.remove(identity, lifetime)
        else:
            raise ValueError("Unknown authorization lifetime")

    def execution_guard(self, session, baseline: PermissionSnapshot, grant=None):
        """Permit additions; removals and hard-policy changes invalidate live work."""
        def validate():
            current = self.snapshot(session)
            for name in ("deny", "ask"):
                if getattr(current.settings, name) != getattr(baseline.settings, name):
                    raise PermissionConflict("Authorization restrictions changed")
            if (current.mode, current.interaction_mode) != (baseline.mode, baseline.interaction_mode):
                raise PermissionConflict("Permission mode changed")
            if current.scope.read_only != baseline.scope.read_only:
                raise PermissionConflict("Read-only boundaries changed")
            previous = {json.dumps(rule.to_persistent(), sort_keys=True) for rule in (*baseline.rules, *baseline.settings.allow)
                        if grant is None or rule.kind == "directory" or (rule.kind in {"shell", "tool"} and rule.tool_name == "execute_command")}
            latest = {json.dumps(rule.to_persistent(), sort_keys=True) for rule in (*current.rules, *current.settings.allow)}
            if not previous.issubset(latest):
                raise PermissionConflict("Authorization was revoked")
        return validate

    def reference_roots(self, session) -> list[str]:
        from ...domain.policy.permission.scope import path_module
        snapshot = self.snapshot(session)
        files = [path_module(rule.resource_root(str(session.workspace_dir))).join(rule.resource_root(str(session.workspace_dir)), rule.pattern)
                 for rule in (*snapshot.rules, *snapshot.settings.allow) if rule.kind == "file" and rule.exact and "file_read" in rule.operations]
        return [*(str(root) for root in snapshot.scope.additional), *files]

    def read_file(self, session, backend, path, *, max_bytes: int) -> bytes:
        """References use the same pure policy and immutable execution permit."""
        from ...domain.model.tool import AccessTarget, ToolAccess, ToolCall
        from ...domain.policy.permission.resolver import (
            PermissionPolicy,
            PermissionResolver,
        )
        from ...domain.policy.permission.types import (
            InvocationIdentity,
            PermissionSubject,
        )
        snapshot = self.snapshot(session)
        resolver = PermissionResolver(PermissionPolicy(snapshot.settings))
        subject = PermissionSubject("read_file", False, lambda args: ToolAccess(
            frozenset({"file_read"}), (AccessTarget("file", args["file"], "file_read", kind="file"),)), lambda _: None)
        decision = resolver.resolve(ToolCall("read_file", {"file": str(path)}, "reference"), subject,
            backend=backend, scope=snapshot.scope, identity=InvocationIdentity(session.session_id), cwd=backend.cwd(),
            session_rules=snapshot.rules, permission_mode=snapshot.mode, interaction_mode=snapshot.interaction_mode)
        if decision.decision != "allow" or decision.grant is None:
            raise PermissionError(decision.reason)
        with self.repository.transaction():
            self.validate(session, snapshot.version)
            if self.execution_factory is None:
                raise RuntimeError("An authorized execution factory must be injected")
            execution = self.execution_factory(backend, decision.grant, validator=lambda: self.validate(session, snapshot.version))
            return execution.read_bytes(backend.resolve_path(str(path)), limit=max_bytes)

    def resolver(self, template, snapshot: PermissionSnapshot, session=None):
        from ...domain.policy.permission.resolver import (
            PermissionPolicy,
            PermissionResolver,
        )

        handler = template.approval_handler
        binder = getattr(handler, "with_validator", None)
        if session is not None and callable(binder):
            handler = binder(lambda: self.validate(session, snapshot.version))
        return PermissionResolver(PermissionPolicy(snapshot.settings), handler, template.interaction_handler)
