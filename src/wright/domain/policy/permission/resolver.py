"""Single permission policy and invocation-authority resolver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from ...gateway.execution import ExecutionPath, PathResolver
from ...model.tool import ToolAccess, ToolCall
from .approval import (
    ResolvedTarget,
)
from .limits import ASK_OPERATIONS, PLAN_OPERATIONS
from .scope import AccessScope, PathClass, is_under
from .settings import (
    MatchContext,
    PermissionConfiguration,
    PermissionRule,
    PermissionSettings,
    parse_http_origin,
    relative_to_root,
)
from .types import (
    AccessTarget,
    AuthorizationChange,
    GrantTarget,
    InvocationGrant,
    InvocationIdentity,
    NetworkTarget,
    PermissionDecision,
    PermissionResolution,
    PermissionSubject,
)


class PermissionPolicy:
    """Hard policy layer; tool names and arguments remain outside this class."""

    _AUTO_DEFAULT = frozenset({"file_read", "internal_read", "plan_update"})
    _EDIT_OPERATIONS = frozenset({"file_read", "file_write"})

    def __init__(self, settings: PermissionSettings | None = None):
        self.settings = PermissionConfiguration.from_settings(settings) if isinstance(settings, PermissionSettings) else settings

    def evaluate(
        self,
        access: ToolAccess,
        *,
        tool_name: str,
        subject: str,
        in_scope: bool,
        context: MatchContext | None = None,
        session_rules: tuple[PermissionRule, ...] = (),
        interaction_mode: str = "agent",
        permission_mode: str | None = None,
    ) -> tuple[PermissionDecision, str, str, str, dict[str, str]]:
        """Return decision, English reason, source, display code, and display params."""

        settings = self.settings
        mode = permission_mode or (settings.mode if settings is not None else "default")
        if interaction_mode == "ask" and (
            "unknown" in access.operations
            or any(operation not in ASK_OPERATIONS for operation in access.operations)
        ):
            return (
                "deny",
                "Ask mode blocks this operation",
                "mode",
                "permission.ask_denied",
                {},
            )
        if interaction_mode == "plan":
            # Plan is a ceiling. Bypass and allow rules cannot widen it.
            mode = "plan"

        # Hard mode restrictions precede configurable rules.  In particular,
        # bypass and an allow rule cannot turn a plan-mode write/MCP/control
        # operation into an executable call.
        if mode == "plan" and (
            "unknown" in access.operations
            or any(operation not in PLAN_OPERATIONS for operation in access.operations)
        ):
            return "deny", "Plan mode blocks this operation", "mode", "permission.plan_denied", {}

        if settings is not None and _matches(settings.deny, tool_name, subject, effect="deny", context=context):
            return (
                "deny",
                f"Matched a deny rule: {tool_name}({subject})",
                "rule_config",
                "permission.deny_rule",
                {"tool_name": tool_name, "subject": subject},
            )
        if settings is not None and _matches(settings.ask, tool_name, subject, effect="ask", context=context):
            return (
                "ask",
                f"Matched an ask rule and needs confirmation: {tool_name}({subject})",
                "rule_config",
                "permission.ask_rule",
                {"tool_name": tool_name, "subject": subject},
            )

        if "shell" in access.operations and access.operations & {"network_read", "network_write"}:
            return "ask", "This command requests network capability", "policy", "permission.needs_approval", {}
        if access.operations & {"external_unknown", "unknown"}:
            return "ask", "Unknown or undeclared external effects need approval for this invocation", "policy", "permission.unknown_operation", {}

        allow_rules = tuple(rule for rule in (*session_rules, *(settings.allow if settings else ()))
                            if in_scope or rule.kind != "tool" or not (context and context.files))
        contexts = tuple(replace(context, files=(file,)) for file in context.files) if context and context.files else (context,)
        allowed = all(_matches(allow_rules, tool_name, subject, effect="allow", context=item) for item in contexts)
        if context and context.files and not access.operations <= {operation for _, operation in context.files}:
            allowed = _matches([rule for rule in allow_rules if rule.kind == "tool"], tool_name, subject, effect="allow", context=context)
        if allowed:
            return (
                "allow",
                f"Matched an allow rule: {tool_name}({subject})",
                "rule_config",
                "permission.allow_rule",
                {"tool_name": tool_name, "subject": subject},
            )

        if mode in {"bypass", "acceptEdits"} and in_scope and access.operations <= self._EDIT_OPERATIONS:
            return "allow", "Workspace editing is enabled", "mode", "permission.accept_edits", {}
        if mode == "bypass" and in_scope and access.operations <= {"shell", "internal_read", "execution_control", "persistent_write", "plan_update", "user_interaction"}:
            return "allow", "Sandbox command approval is disabled", "mode", "permission.bypass_allow", {}

        if "unknown" in access.operations:
            return (
                "ask",
                "An undeclared tool operation needs explicit approval",
                "policy",
                "permission.unknown_operation",
                {},
            )
        if not in_scope and any(
            operation in {"file_read", "file_write"}
            for operation in access.operations
        ):
            return (
                "ask",
                "The target is outside this session's access scope",
                "scope",
                "permission.outside_scope",
                {},
            )

        if access.operations <= self._AUTO_DEFAULT and in_scope:
            return (
                "allow",
                "The default policy allows in-scope reads and internal queries",
                "default",
                "permission.default_allow",
                {},
            )
        return "ask", "This operation needs approval", "policy", "permission.needs_approval", {}

    def apply(
        self,
        access: ToolAccess,
        *,
        tool_name: str,
        subject: str,
        in_scope: bool,
        context: MatchContext | None = None,
        session_rules: tuple[PermissionRule, ...] = (),
        interaction_mode: str = "agent",
        permission_mode: str | None = None,
    ) -> tuple[PermissionDecision, str, str, str, dict[str, str]]:
        return self.evaluate(
            access,
            tool_name=tool_name,
            subject=subject,
            in_scope=in_scope,
            context=context,
            session_rules=session_rules,
            interaction_mode=interaction_mode,
            permission_mode=permission_mode,
        )



@dataclass(frozen=True)
class PreparedPermission:
    arguments: dict
    access: ToolAccess
    targets: tuple[ResolvedTarget, ...]
    cwd: ExecutionPath
    identity: InvocationIdentity
    scope: AccessScope
    remember_rule: PermissionRule | None
    decision: PermissionDecision
    reason: str
    source: str
    reason_code: str
    reason_params: tuple[tuple[str, str], ...]

class PermissionResolver:
    """Pure resource preparation and invocation authority construction."""

    def __init__(self, policy: PermissionPolicy | None = None):
        self.policy = policy or PermissionPolicy()

    def prepare(
        self,
        tool_call: ToolCall,
        subject: PermissionSubject,
        *,
        backend: PathResolver | None,
        scope: AccessScope,
        identity: InvocationIdentity,
        cwd: ExecutionPath | None = None,
        session_rules: tuple[PermissionRule, ...] = (),
        interaction_mode: str = "agent",
        permission_mode: str | None = None,
    ) -> PermissionResolution:
        arguments = dict(tool_call.arguments)
        if backend is not None:
            fixed_cwd = cwd or backend.cwd()
        elif cwd is not None:
            fixed_cwd = cwd
        else:
            return self._deny(
                arguments,
                "No fixed execution environment is available",
                source="resolver",
                reason_code="permission.no_environment",
            )

        try:
            access = subject.describe_access(arguments)
            if not isinstance(access, ToolAccess) or not access.operations:
                raise TypeError("describe_access must return ToolAccess with operations")
        except Exception as exc:
            return self._deny(
                arguments,
                f"{subject.name}: access description failed: {type(exc).__name__}: {exc}",
                source="access_description_error",
            )

        if "shell" in access.operations and not scope.contains(fixed_cwd.value):
            return self._deny(arguments, "Authorize the working directory before running Shell", source="scope", reason_code="permission.outside_scope")
        try:
            _blocked_paths(backend, fixed_cwd, scope.protected)
        except Exception as exc:
            return self._deny(arguments, f"Protected boundary could not be resolved: {exc}", source="protected_path", reason_code="permission.protected_path")
        resolved = self._resolve_targets(access.targets, backend, fixed_cwd, scope)
        if resolved is None:
            return self._deny(
                arguments,
                "The tool resource could not be resolved",
                source="resolver",
                reason_code="permission.unresolved_target",
            )
        forbidden = next(
            (item for item in resolved if item.classification == PathClass.FORBIDDEN),
            None,
        )
        if forbidden is not None:
            return self._deny(
                arguments,
                "The target is a protected permission file and cannot be accessed directly",
                reason_code="permission.protected_path",
                risk_flags=(*access.risk_flags, "protected_path"),
                source="protected_path",
            )

        in_scope = all(
            item.classification in {None, PathClass.IN_ORIGIN, PathClass.IN_GRANTED}
            for item in resolved
        )
        effective_access = access
        if not in_scope and "path_outside_scope" not in access.risk_flags:
            effective_access = replace(
                access, risk_flags=(*access.risk_flags, "path_outside_scope")
            )
        access_subject = access.subject or _subject_from_arguments(arguments)
        if effective_access.subject != access_subject:
            effective_access = replace(effective_access, subject=access_subject)
        sandbox_key = hashlib.sha256(json.dumps([str(path) for path in (*scope.roots, *scope.read_only, *scope.protected)]).encode()).hexdigest()
        context = replace(_match_context(arguments, resolved, scope), cwd=fixed_cwd.value, sandbox_key=sandbox_key)
        all_rules = (*session_rules, *(self.policy.settings.allow if self.policy.settings else ()))
        for rule in all_rules:
            root = rule.resource_root(str(scope.origin))
            if rule.kind == "directory" and "file_write" not in rule.operations and any(
                item.path is not None and item.declaration.operation == "file_write" and is_under(item.path.value, root)
                for item in resolved
            ):
                return self._deny(arguments, "This directory is read-only", source="scope", reason_code="permission.read_only_directory")
        decision, reason, source, reason_code, reason_params = self.policy.apply(
            effective_access,
            tool_name=subject.name,
            subject=access_subject,
            in_scope=in_scope,
            context=context,
            session_rules=session_rules,
            interaction_mode=interaction_mode,
            permission_mode=permission_mode,
        )

        remember_rule = _rememberable_rule(
            subject.name, arguments, effective_access, resolved, scope
        )
        if "shell" in effective_access.operations and not effective_access.operations & {"network_read", "network_write"}:
            remember_rule = PermissionRule(subject.name, kind="shell", pattern=access_subject, exact=True, cwd=fixed_cwd.value, sandbox_key=sandbox_key)

        if subject.requires_user_interaction and decision != "deny":
            decision = "ask"
            if not reason:
                reason = "This action needs the user"
                reason_code = "permission.user_interaction"
                reason_params = {}
            source = "user_interaction"

        return PreparedPermission(arguments, effective_access, resolved, fixed_cwd, identity, scope, remember_rule, decision, reason, source, reason_code, _pairs(reason_params))

    def _resolve_targets(
        self,
        targets: tuple[AccessTarget, ...],
        backend: PathResolver | None,
        cwd: ExecutionPath,
        scope: AccessScope,
    ) -> tuple[ResolvedTarget, ...] | None:
        result: list[ResolvedTarget] = []
        for target in targets:
            if target.kind in {"file", "directory"}:
                if backend is None:
                    return None
                try:
                    path = backend.resolve_path(target.value, cwd=cwd)
                except Exception:
                    return None
                result.append(ResolvedTarget(target, path, scope.classify(path.value)))
            else:
                result.append(ResolvedTarget(target, None, None))
        return tuple(result)


    def issue(
        self,
        arguments: dict,
        access: ToolAccess,
        targets: tuple[ResolvedTarget, ...],
        cwd: ExecutionPath,
        identity: InvocationIdentity,
        reason: str,
        source: str,
        *,
        backend: PathResolver | None,
        scope: AccessScope,
        changes: AuthorizationChange | None = None,
        network_enabled: bool = False,
        reason_code: str = "",
        reason_params: dict[str, str] | None = None,
    ) -> PermissionResolution:
        grant_targets = tuple(
            GrantTarget(item.path, item.declaration.operation, item.declaration.recursive)
            for item in targets
            if item.path is not None
        )
        grant = InvocationGrant(
            identity,
            cwd.environment_id,
            cwd,
            access.operations,
            grant_targets,
            access.subject,
            access.subject if "shell" in access.operations else None,
            _blocked_paths(backend, cwd, scope.protected),
            tuple(backend.resolve_path(str(root), cwd=cwd) for root in scope.roots) if backend else (),
            tuple(backend.resolve_path(str(root), cwd=cwd) for root in scope.roots
                  if not any(is_under(str(root), str(readonly)) for readonly in scope.read_only)) if backend else (),
            network_enabled,
            shell_readonly=tuple(ExecutionPath(cwd.environment_id, str(path)) for path in scope.read_only),
            network_targets=tuple(NetworkTarget(str(item.declaration.value), item.declaration.http_method or "GET", item.declaration.operation) for item in targets if item.declaration.kind == "url"),

        )
        return PermissionResolution(
            arguments,
            "allow",
            reason,
            access.risk_flags,
            source,
            grant,
            changes or AuthorizationChange(),
            reason_code=reason_code,
            reason_params=_pairs(reason_params or {}),
        )


    @staticmethod
    def _deny(
        arguments: dict,
        reason: str,
        *,
        risk_flags: tuple[str, ...] = (),
        source: str,
        reason_code: str = "",
        reason_params: dict[str, str] | None = None,
    ) -> PermissionResolution:
        return PermissionResolution(
            arguments, "deny", reason, risk_flags, source,
            reason_code=reason_code,
            reason_params=_pairs(reason_params or {}),
        )


def _pairs(params: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple((key, str(value)) for key, value in params.items())


def _matches(rules, tool_name: str, subject: str, *, effect: str, context: MatchContext | None) -> bool:
    return any(
        rule.matches(tool_name, subject, effect=effect, context=context)
        for rule in rules
    )


def _subject_from_arguments(arguments: dict) -> str:
    for key in ("command", "file", "directory", "path", "url"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True)


def _rememberable_rule(
    tool_name: str,
    arguments: dict,
    access: ToolAccess,
    resolved: tuple[ResolvedTarget, ...],
    scope: AccessScope,
) -> PermissionRule | None:
    """Return a bounded rule. The pattern is built from the canonical path."""

    if access.operations & {
        "shell",
        "network_write",
        "external_unknown",
        "execution_control",
        "persistent_write",
    }:
        return None
    if {
        "executes_shell",
        "deletes_files",
        "modifies_git_state",
        "mutates_remote_state",
        "network_fetch",
        "package_manager",
    } & set(access.risk_flags):
        return None

    file_targets = [
        item for item in resolved
        if item.path is not None and item.declaration.kind == "file"

    ]
    paths = {item.path.value for item in file_targets if item.path is not None}
    if len(paths) == 1:
        item = file_targets[0]
        assert item.path is not None
        root = _containing_root(item.path.value, scope) or item.path.parent.value
        relative = relative_to_root(item.path.value, root) if root else None
        if root and relative and ".." not in PurePosixPath(relative).parts:
            pattern = relative
            operations = tuple(sorted({
                target.declaration.operation for target in file_targets
            }))
            return replace(PermissionRule.file_grant("*", root, pattern, operations), exact=True)

    directory_targets = [item for item in resolved if item.path is not None and item.declaration.kind == "directory"]
    if len(directory_targets) == 1:
        item = directory_targets[0]
        return PermissionRule("*", kind="directory", root=item.path.value, operations=("file_read",), recursive=True)

    url_value = arguments.get("url")
    if isinstance(url_value, str) and url_value and "network_read" in access.operations:
        origin = parse_http_origin(url_value)
        method = str(arguments.get("method", "GET")).upper()
        if origin is not None and method in {"GET", "HEAD", "OPTIONS"}:
            scheme, host, port = origin
            return PermissionRule.network_grant(tool_name, scheme, host, port, (method,))
    return None


def _containing_root(path: str, scope: AccessScope) -> str | None:
    roots = [str(scope.origin)]
    roots.extend(str(item) for item in scope.additional)
    containing = [root for root in roots if path == root or is_under(path, root)]
    if not containing:
        return None
    return max(containing, key=len)


def _match_context(
    arguments: dict,
    resolved: tuple[ResolvedTarget, ...],
    scope: AccessScope,
) -> MatchContext:
    files = tuple(
        (item.path.value, item.declaration.operation)
        for item in resolved
        if item.path is not None and item.declaration.kind in {"file", "directory"}
    )
    url = ""
    for item in resolved:
        if item.declaration.kind == "url" and isinstance(item.declaration.value, str):
            url = item.declaration.value
            break
    if not url:
        raw_url = arguments.get("url")
        url = raw_url if isinstance(raw_url, str) else ""
    method = next((item.declaration.http_method for item in resolved if item.declaration.http_method), arguments.get("method", "GET"))
    return MatchContext(
        session_origin=str(scope.origin),
        files=files,
        url=url,
        method=str(method).upper() if isinstance(method, str) else "GET",
    )


def _permission_shape(access: ToolAccess) -> tuple:
    return (
        access.operations,
        tuple((target.kind, target.operation, target.value, target.recursive, target.http_method) for target in access.targets),
        access.subject,
    )


def _blocked_paths(
    backend: PathResolver | None,
    cwd: ExecutionPath,
    protected: tuple[Path, ...],
) -> tuple[ExecutionPath, ...]:
    if backend is None:
        return ()
    paths: list[ExecutionPath] = []
    for forbidden in protected:
        path = backend.resolve_path(str(forbidden), cwd=cwd)
        if path not in paths:
            paths.append(path)
    return tuple(paths)
