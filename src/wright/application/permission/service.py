"""Application authorization lifecycle shared by interactive and durable calls."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ...domain.gateway.permissions import PermissionRepository
from ...domain.model.session.authorization import AuthorizationState
from ...domain.model.tool import AccessTarget, ToolAccess, ToolCall
from ...domain.policy.permission.resolver import PermissionPolicy
from ...domain.policy.permission.scope import AccessScope, path_module
from ...domain.policy.permission.settings import (
    PermissionConfiguration,
    PermissionRule,
    command_segments,
)
from ...domain.policy.permission.types import (
    AuthorizationChange,
    InvocationIdentity,
    PermissionSubject,
)
from .checkpoint import CheckpointParticipant
from .resources import resource_change
from .workflow import AuthorizationWorkflow


class PermissionConflict(ValueError):
    pass


@dataclass(frozen=True)
class PermissionSnapshot:
    settings: PermissionConfiguration
    rules: tuple[PermissionRule, ...]
    scope: AccessScope
    version: str
    mode: str
    interaction_mode: str
    inheritance: tuple[AuthorizationState, ...]


def record_id(record: dict) -> str:
    return str(
        record.get("id")
        or hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:24]
    )


class PermissionService:
    def __init__(self, repository: PermissionRepository, *, execution_factory=None, network_factory=None, backend_factory=None):
        self._repository = repository
        self.execution_factory = execution_factory
        self.backend_factory = backend_factory
        self._cache = {}
        self._checkpoints: dict[str, CheckpointParticipant] = {}
        self.policy_updated = None
        self.network_factory = network_factory

    def for_background_execution(self):
        """A checkpoint-free lifecycle sharing the same authoritative repository."""
        return PermissionService(self._repository, execution_factory=self.execution_factory, network_factory=self.network_factory, backend_factory=self.backend_factory)

    def sessionless_scope(self, workspace: Path) -> AccessScope:
        return AccessScope(workspace, protected=tuple(Path(path) for path in self._repository.protected_paths()))

    def bind_checkpoint(self, session_id: str, participant: CheckpointParticipant) -> None:
        self._checkpoints[session_id] = participant

    def _save_checkpoint(self, session) -> None:
        participant = self._checkpoints.get(session.authorization.owner().session_id)
        if participant is not None:
            participant.save()

    def _notify(self, session, payload=None) -> None:
        if self.policy_updated is not None:
            self.policy_updated(session.authorization.owner().session_id, payload or {})

    @staticmethod
    def _records(rules: tuple[PermissionRule, ...]) -> tuple[dict, ...]:
        return tuple(rule.to_persistent() for rule in rules)

    def set_modes(self, session, *, interaction_mode=None, permission_mode=None, execution_mode=None) -> None:
        if execution_mode is not None and execution_mode not in {"sandbox", "host"}:
            raise ValueError("execution_mode must be sandbox or host")
        with self._repository.transaction(), session.authorization.locked_lineage():
            authorization = session.authorization
            before = authorization.snapshot()
            previous_execution_mode = session.execution_mode
            try:
                with self._repository.unit_of_work():
                    authorization.set_modes(
                        permission_mode=before.permission_mode
                        if permission_mode is None
                        else permission_mode,
                        interaction_mode=before.interaction_mode
                        if interaction_mode is None
                        else interaction_mode,
                    )
                    if execution_mode is not None:
                        session.execution_mode = execution_mode
                    self._save_checkpoint(session)
            except Exception:
                session.execution_mode = previous_execution_mode
                authorization.set_modes(
                    permission_mode=before.permission_mode,
                    interaction_mode=before.interaction_mode,
                )
                self._cache.clear()
                raise
            self._cache.pop(session.session_id, None)
        self._notify(session, {"session_id": session.session_id, "interaction_mode": authorization.interaction_mode,
            "permission_mode": authorization.permission_mode, "execution_mode": session.execution_mode, "effective": "next permission resolution"})

    def restore(self, session, backend) -> PermissionSnapshot:
        """Refresh current stores before any execution; checkpoint grants are seeds only."""
        with self._repository.transaction():
            snapshot = self._snapshot(session)
            origin = backend.resolve_path(str(session.workspace_dir))
            if (
                backend.metadata(origin).kind != "directory"
                or snapshot.scope.classify(origin.value).value == "forbidden"
            ):
                raise ValueError("Execution root is unavailable or protected")
            cwd = backend.resolve_path(str(session.get_cwd()))
            if backend.metadata(cwd).kind != "directory" or snapshot.scope.classify(
                cwd.value
            ).value in {"outside", "forbidden"}:
                cwd = backend.resolve_path(str(session.workspace_dir))
            session.set_cwd(Path(cwd.value))
            return snapshot

    def snapshot(self, session) -> PermissionSnapshot:
        with self._repository.transaction():
            return self._snapshot(session)

    def _snapshot(self, session) -> PermissionSnapshot:
        with session.authorization.locked_lineage() as nodes:
            for _ in range(3):
                key = self._token(session)
                cached = self._cache.get(session.session_id)
                if cached is not None and cached[0] == key:
                    return cached[1]
                snapshot = self._load_snapshot(session, nodes)
                final_key = self._token(session)
                if key[0] == final_key[0]:
                    self._cache[session.session_id] = (final_key, snapshot)
                    return snapshot
            raise PermissionConflict("Permissions changed while capturing their context")

    def _load_snapshot(self, session, nodes) -> PermissionSnapshot:
        settings, protected = self._repository.read()
        authorization = session.authorization
        session_versions = []
        for ancestor in nodes:
            latest, revision = self._repository.read_session(
                ancestor.session_id, self._records(ancestor.rules)
            )
            ancestor.replace_rules(PermissionRule.from_mapping(rule) for rule in latest)
            session_versions.append((ancestor.session_id, revision))
        states = tuple(item.snapshot() for item in nodes)
        records = tuple(rule for state in states for rule in state.rules)
        modes = [(state.permission_mode, state.interaction_mode) for state in states]
        mode, interaction = authorization.combine_modes(states, settings.mode)
        directories = [
            Path(rule.resource_root(str(session.workspace_dir)))
            for rule in (*settings.allow, *records)
            if rule.kind == "directory"
        ]
        writable = tuple(
            Path(rule.resource_root(str(session.workspace_dir)))
            for rule in (*settings.allow, *records)
            if rule.kind == "directory" and "file_write" in rule.operations
        )
        scope = AccessScope(
            session.workspace_dir,
            tuple(directories),
            tuple(Path(path) for path in protected),
            session.project_root or session.workspace_dir,
            writable,
        )
        payload = {
            "session_revisions": session_versions,
            "revision": settings.revision,
            "settings": {
                key: [rule.to_persistent() for rule in getattr(settings, key)]
                for key in ("allow", "deny", "ask")
            },
            "session": [rule.to_persistent() for rule in records],
            "mode": mode,
            "execution_mode": session.execution_mode,
            "ancestor_modes": modes,
            "interaction": interaction,
            "workspace": str(session.workspace_dir),
            "protected": tuple(str(p) for p in scope.protected),
            "writable": tuple(str(p) for p in scope.writable_roots),
            "project_root": str(scope.project_root),
        }
        version = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        snapshot = PermissionSnapshot(PermissionConfiguration.from_settings(settings), records, scope, version, mode, interaction, states)
        return snapshot

    def _token(self, session):
        states = tuple(item.snapshot() for item in session.authorization.lineage())
        return (self._repository.revision_token(tuple(state.session_id for state in states)), states, str(session.workspace_dir), str(session.project_root), session.execution_mode)

    def validate(self, session, version: str) -> None:
        if self.snapshot(session).version != version:
            raise PermissionConflict("Permissions changed; request approval again")

    def commit(self, change: AuthorizationChange, session, *, expected_version: str | None = None) -> None:
        with self._repository.transaction(), session.authorization.locked_lineage() as nodes:
            owner = nodes[-1]
            if expected_version is not None:
                self.validate(session, expected_version)
            self.snapshot(session)
            before = owner.rules
            try:
                with self._repository.unit_of_work():
                    for lifetime, rules in (("session", change.session_rules), ("project", change.project_rules), ("user", change.persistent_rules)):
                        stamped = tuple(replace(rule, id=uuid4().hex, lifetime=lifetime,
                            project_id=self._repository.project_identity if lifetime != "user" else "") for rule in rules)
                        for rule in stamped:
                            rule.validate_grant()
                        if lifetime == "session":
                            for rule in stamped:
                                owner.add_rule(rule)
                        elif stamped:
                            self._repository.add(self._records(stamped), lifetime)
                    if change.session_rules:
                        self._repository.write_session(owner.session_id, self._records(owner.rules))
                    self._save_checkpoint(session)
            except Exception:
                owner.replace_rules(before)
                self._cache.clear()
                raise
        self._notify(session)

    def revoke(self, session, identity: str, lifetime: str, version: str) -> None:
        with self._repository.transaction(), session.authorization.locked_lineage() as nodes:
            owner = nodes[-1]
            self.validate(session, version)
            snapshot = self.snapshot(session)
            before = owner.rules
            try:
                with self._repository.unit_of_work():
                    if lifetime == "session":
                        if not owner.revoke(identity):
                            raise ValueError("Authorization not found")
                        self._repository.write_session(owner.session_id, self._records(owner.rules))
                    elif lifetime in {"project", "user"}:
                        if not any(rule.lifetime == lifetime and record_id(rule.to_persistent()) == identity for rule in snapshot.settings.allow):
                            raise ValueError("Authorization not found")
                        self._repository.remove(identity, lifetime)
                    else:
                        raise ValueError("Unknown authorization lifetime")
                    self._save_checkpoint(session)
            except Exception:
                owner.replace_rules(before)
                self._cache.clear()
                raise
        self._notify(session)

    def execution_guard(self, session, baseline: PermissionSnapshot, grant=None):
        """Permit additions; removals and hard-policy changes invalidate live work."""

        execution_mode = session.execution_mode

        def validate():
            if session.execution_mode != execution_mode:
                raise PermissionConflict("Execution mode changed")
            current = self.snapshot(session)
            for name in ("deny", "ask"):
                if getattr(current.settings, name) != getattr(baseline.settings, name):
                    raise PermissionConflict("Authorization restrictions changed")
            if (current.mode, current.interaction_mode) != (
                baseline.mode,
                baseline.interaction_mode,
            ):
                raise PermissionConflict("Permission mode changed")
            if current.scope.protected != baseline.scope.protected:
                raise PermissionConflict("Protected boundaries changed")
            if (current.scope.origin, current.scope.project_root) != (
                baseline.scope.origin, baseline.scope.project_root
            ):
                raise PermissionConflict("Execution boundaries changed")
            if not set(baseline.scope.writable_roots) <= set(current.scope.writable_roots):
                raise PermissionConflict("Writable boundaries changed")
            previous = {
                json.dumps(rule.to_persistent(), sort_keys=True)
                for rule in (*baseline.rules, *baseline.settings.allow)
                if grant is None or rule.kind == "directory" or _shell_depends_on(rule, grant)
            }
            latest = {
                json.dumps(rule.to_persistent(), sort_keys=True)
                for rule in (*current.rules, *current.settings.allow)
            }
            if not previous.issubset(latest):
                raise PermissionConflict("Authorization was revoked")

        return validate

    def reference_roots(self, session) -> list[str]:
        snapshot = self.snapshot(session)
        files = [
            path_module(rule.resource_root(str(session.workspace_dir))).join(
                rule.resource_root(str(session.workspace_dir)), rule.pattern
            )
            for rule in (*snapshot.rules, *snapshot.settings.allow)
            if rule.kind == "file" and rule.exact and "file_read" in rule.operations
        ]
        return [*(str(root) for root in snapshot.scope.additional), *files]

    def authorize(self, session, tool_call, subject, *, backend, workflow=None, cwd=None, scope=None, identity=None, requires_execution=False, requires_network=False):
        """The sole approval, commit, and permit-issuance workflow."""
        template = workflow or AuthorizationWorkflow()
        snapshot = self.snapshot(session) if session is not None else None
        if snapshot is not None:
            handler = template.approval_handler
            binder = getattr(handler, "with_validator", None)
            if callable(binder):
                handler = binder(lambda: self.validate(session, snapshot.version))
            active = AuthorizationWorkflow(PermissionPolicy(snapshot.settings), handler, template.interaction_handler, execution_mode=session.execution_mode)
            scope = snapshot.scope
        else:
            active = template
        identity = identity or InvocationIdentity(session.session_id if session is not None else "", tool_call.id, tool_call.id,
            getattr(session, "agent_task_id", None))
        resolution = active.resolve(tool_call, subject, backend=backend, scope=scope, identity=identity,
            cwd=cwd or backend.cwd(), session_rules=snapshot.rules if snapshot else (),
            permission_mode=snapshot.mode if snapshot else None, interaction_mode=snapshot.interaction_mode if snapshot else "agent")
        if resolution.decision != "allow" or resolution.grant is None:
            return AuthorizedInvocation(resolution, snapshot, scope)
        with self._repository.transaction(), (session.authorization.locked_lineage() if session is not None else nullcontext()):
            if snapshot is not None:
                self.validate(session, snapshot.version)
                if resolution.changes != AuthorizationChange():
                    self.commit(resolution.changes, session, expected_version=snapshot.version)
                snapshot = self.snapshot(session)
            elif resolution.changes != AuthorizationChange():
                raise PermissionError("Remembered authorization requires a session")
            # The version is an optimistic concurrency token for approval and
            # commit. An issued invocation remains valid across additive grants,
            # but must still observe revocation and changed restrictions.
            validator = self.execution_guard(session, snapshot) if snapshot else None
            grant = replace(resolution.grant, policy_version=snapshot.version if snapshot else "")
            resolution = replace(resolution, grant=grant)
            execution = None
            if requires_execution:
                if self.execution_factory is None:
                    raise RuntimeError("An authorized execution factory must be injected")
                execution = self.execution_factory(backend, grant, validator=validator,
                    process_validator=self.execution_guard(session, snapshot, grant) if snapshot else None)
            network = None
            if requires_network:
                if self.network_factory is None:
                    raise RuntimeError("An authorized network factory must be injected")
                network = self.network_factory(grant, validator=validator)
            return AuthorizedInvocation(resolution, snapshot, snapshot.scope if snapshot else scope, execution, network, validator)

    def grant_resource(self, session, backend, resource, lifetime, expected_version):
        with self._repository.transaction():
            self.validate(session, expected_version)
            change = resource_change(session, backend, self.snapshot(session), resource, lifetime)
            self.commit(change, session, expected_version=expected_version)

    def read_file(self, session, backend, path, *, max_bytes: int) -> bytes:
        subject = PermissionSubject("read_file", False,
            lambda args: ToolAccess(frozenset({"file_read"}), (AccessTarget("file", args["file"], "file_read", kind="file"),)), lambda _: None)
        invocation = self.authorize(session, ToolCall("read_file", {"file": str(path)}, "reference"), subject,
            backend=backend, requires_execution=True)
        if invocation.execution is None:
            raise PermissionError(invocation.resolution.reason)
        try:
            return invocation.execution.read_bytes(backend.resolve_path(str(path)), limit=max_bytes)
        finally:
            invocation.execution.close()


def _shell_depends_on(rule: PermissionRule, grant) -> bool:
    """Whether revoking this rule withdraws what let this Shell command start."""
    if rule.tool_name != "execute_command":
        return False
    if rule.kind == "sandbox_network":
        return bool(grant.network_enabled)
    if rule.kind == "command":
        programs = command_segments(grant.command or "") or ()
        return PurePosixPath(rule.argv[0]).name in {PurePosixPath(argv[0]).name for argv in programs}
    return rule.kind == "tool" or (rule.kind == "shell" and rule.pattern == grant.command)


@dataclass(frozen=True)
class AuthorizedInvocation:
    resolution: object
    snapshot: PermissionSnapshot | None
    scope: AccessScope
    execution: object = None
    network: object = None
    validator: object = None
