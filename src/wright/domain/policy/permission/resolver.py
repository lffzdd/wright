"""Single permission policy and invocation-authority resolver."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ...gateway.execution import ExecutionPath, PathResolver
from ...model.tool import ToolAccess, ToolCall
from .approval import (
    PermissionApprovalHandler,
    PermissionRequest,
    ResolvedTarget,
    UserInteractionHandler,
    normalize_response,
)
from .scope import AccessScope, PathClass, forbidden_paths, is_under
from .settings import (
    MatchContext,
    PermissionRule,
    parse_http_origin,
    redact_http_target,
    relative_to_root,
)
from .types import (
    AccessTarget,
    AuthorizationChange,
    GrantTarget,
    InvocationGrant,
    InvocationIdentity,
    PermissionChoice,
    PermissionDecision,
    PermissionPrompt,
    PermissionResolution,
    PermissionSubject,
)

if TYPE_CHECKING:
    from .settings import PermissionSettings


class PermissionPolicy:
    """Hard policy layer; tool names and arguments remain outside this class."""

    _AUTO_DEFAULT = frozenset({"file_read", "internal_read", "plan_update"})
    _EDIT_OPERATIONS = frozenset({"file_read", "file_write"})
    _ASK_OPERATIONS = frozenset({
        "file_read", "internal_read", "network_read", "user_interaction",
    })
    _PLAN_OPERATIONS = _ASK_OPERATIONS | frozenset({"plan_update"})

    def __init__(self, settings: PermissionSettings | None = None):
        self.settings = settings

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
            or any(operation not in self._ASK_OPERATIONS for operation in access.operations)
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
            or any(operation not in self._PLAN_OPERATIONS for operation in access.operations)
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

        if mode == "bypass":
            return "allow", "Bypass mode allows this operation", "mode", "permission.bypass_allow", {}
        if mode == "acceptEdits" and access.operations <= self._EDIT_OPERATIONS:
            if in_scope:
                return (
                    "allow",
                    "acceptEdits mode allows this in-scope file edit",
                    "mode",
                    "permission.accept_edits",
                    {},
                )

        if (
            settings is not None and _matches(
                settings.allow, tool_name, subject, effect="allow", context=context
            )
        ) or _matches(session_rules, tool_name, subject, effect="allow", context=context):
            return (
                "allow",
                f"Matched an allow rule: {tool_name}({subject})",
                "rule_config",
                "permission.allow_rule",
                {"tool_name": tool_name, "subject": subject},
            )

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


class PermissionResolver:
    """Resolve a ToolAccess description into a one-call InvocationGrant."""

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        approval_handler: PermissionApprovalHandler | None = None,
        interaction_handler: UserInteractionHandler | None = None,
        *,
        settings: PermissionSettings | None = None,
    ):
        self.policy = policy or PermissionPolicy(settings)
        if settings is not None and self.policy.settings is None:
            self.policy = PermissionPolicy(settings)
        self.approval_handler = approval_handler
        self.interaction_handler = interaction_handler

    def resolve(
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
        _rewrite_depth: int = 0,
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
        context = _match_context(arguments, resolved, scope)
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

        if subject.requires_user_interaction and decision != "deny":
            decision = "ask"
            if not reason:
                reason = "This action needs the user"
                reason_code = "permission.user_interaction"
                reason_params = {}
            source = "user_interaction"

        prompt = self._prompt(
            tool_call,
            subject.name,
            effective_access,
            resolved,
            identity,
            reason,
            remember_rule=remember_rule,
            subject=access_subject,
            cwd=fixed_cwd,
            reason_code=reason_code,
            reason_params=reason_params,
        )
        request = PermissionRequest(
            tool_call,
            arguments,
            effective_access,
            resolved,
            fixed_cwd,
            scope,
            identity,
            prompt,
        )

        if decision == "deny":
            return PermissionResolution(
                arguments, "deny", reason, effective_access.risk_flags, source,
                prompt=prompt, reason_code=reason_code, reason_params=_pairs(reason_params),
            )
        if decision == "allow":
            return self._allowed(
                arguments,
                effective_access,
                resolved,
                fixed_cwd,
                identity,
                reason,
                source,
                backend=backend,
                remember_rule=remember_rule,
                reason_code=reason_code,
                reason_params=reason_params,
            )

        handler = (
            self.interaction_handler
            if subject.requires_user_interaction
            else self.approval_handler
        )
        if handler is None:
            return PermissionResolution(
                arguments,
                "deny",
                f"{reason}; no interaction adapter is available",
                effective_access.risk_flags,
                "no_interaction",
                prompt=prompt,
                reason_code="permission.no_adapter",
                reason_params=_pairs({"reason": reason}),
            )
        try:
            response = normalize_response(handler(request))
        except Exception as exc:
            return PermissionResolution(
                arguments,
                "deny",
                f"The permission adapter failed: {type(exc).__name__}: {exc}",
                effective_access.risk_flags,
                "approval_error",
                prompt=prompt,
                reason_code="permission.adapter_failed",
                reason_params=_pairs({"error_type": type(exc).__name__, "error": str(exc)}),
            )
        if response.choice in {"ask", "abstain", "deny", ""}:
            return PermissionResolution(
                arguments,
                "deny",
                f"Approval denied: {response.choice or 'empty choice'}",
                effective_access.risk_flags,
                "approval",
                prompt=prompt,
                reason_code="permission.approval_denied",
                reason_params=_pairs({"choice": response.choice or "empty choice"}),
            )
        if response.choice not in {choice.id for choice in prompt.choices}:
            return PermissionResolution(
                arguments,
                "deny",
                f"Approval returned a choice this request did not offer: {response.choice}",
                effective_access.risk_flags,
                "invalid_choice",
                prompt=prompt,
                reason_code="permission.invalid_choice",
                reason_params=_pairs({"choice": response.choice}),
            )

        final_arguments = dict(response.updated_arguments or arguments)
        if response.updated_arguments is not None:
            try:
                updated_access = subject.describe_access(final_arguments)
            except Exception as exc:
                return self._deny(
                    final_arguments,
                    f"The rewritten access description failed: {exc}",
                    reason_code="permission.updated_access_failed",
                    reason_params={"error": str(exc)},
                    source="updated_access_description_error",
                )
            validation_error = subject.validate(final_arguments)
            if validation_error is not None:
                return self._deny(
                    final_arguments,
                    f"The rewritten arguments are invalid: {validation_error.err}",
                    reason_code="permission.updated_arguments_invalid",
                    reason_params={"error": validation_error.err},
                    source="updated_arguments_invalid",
                )
            if _permission_shape(updated_access) != _permission_shape(access):
                # The old decision cannot authorize a changed path, command,
                # URL, operation, or subject.  One explicit re-evaluation is
                # allowed; a second rewrite is rejected by the executor.
                updated_call = type(tool_call)(tool_call.name, final_arguments, tool_call.id)
                if _rewrite_depth >= 1:
                    return self._deny(
                        final_arguments,
                        "The approver rewrote permission-related arguments a second time, so this call was denied",
                        reason_code="permission.rewrite_loop",
                        source="rewrite_loop",
                    )
                return self._resolve_updated(
                    updated_call,
                    subject,
                    backend=backend,
                    scope=scope,
                    identity=identity,
                    cwd=fixed_cwd,
                    session_rules=session_rules,
                    interaction_mode=interaction_mode,
                    permission_mode=permission_mode,
                    _rewrite_depth=_rewrite_depth + 1,
                )

        resolution = self._allowed(
            final_arguments,
            effective_access,
            resolved,
            fixed_cwd,
            identity,
            f"{reason}; approved by {response.choice}",
            "approval",
            backend=backend,
            remember_rule=remember_rule,
            choice=response.choice,
        )
        return resolution

    def _resolve_updated(self, *args, **kwargs) -> PermissionResolution:
        # A rewritten request is deliberately resolved from scratch.  The
        # executor tracks one rewrite round and rejects a second resource
        # change rather than allowing an approval loop.
        return self.resolve(*args, **kwargs)

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

    def _prompt(
        self,
        tool_call: ToolCall,
        tool_name: str,
        access: ToolAccess,
        targets: tuple[ResolvedTarget, ...],
        identity: InvocationIdentity,
        reason: str,
        *,
        remember_rule: PermissionRule | None,
        subject: str,
        cwd: ExecutionPath,
        reason_code: str = "",
        reason_params: dict[str, str] | None = None,
    ) -> PermissionPrompt:
        outside = any(item.classification == PathClass.OUTSIDE for item in targets)
        directories = _outside_directories(targets)
        directory_scope = _directory_grant_text(directories)
        rule_scope = remember_rule.describe() if remember_rule is not None else ""
        if outside:
            choices = (
                PermissionChoice("allow_once", "Allow once", "This invocation only", "No save"),
                PermissionChoice(
                    "allow_session_directory",
                    "Allow directory for this session",
                    directory_scope,
                    "Save in this session checkpoint",
                ),
                PermissionChoice(
                    "allow_persistent_directory",
                    "Allow directory permanently",
                    directory_scope,
                    "Save in user permissions for later sessions",
                ),
                PermissionChoice("deny", "Deny", "No execution", "No save"),
            )
            grant_summary = directory_scope
            summary_code = "permission.scope.directory"
            summary_params = {"directories": "; ".join(directories) or "(no directory)"}
        elif remember_rule is not None:
            choices = (
                PermissionChoice("allow_once", "Allow once", "This invocation only", "No save"),
                PermissionChoice(
                    "allow_session_rule",
                    "Allow this rule for the session",
                    rule_scope,
                    "Save on this session only",
                ),
                PermissionChoice(
                    "allow_persistent_rule",
                    "Allow this rule permanently",
                    rule_scope,
                    "Save in user permissions for later sessions",
                ),
                PermissionChoice("deny", "Deny", "No execution", "No save"),
            )
            grant_summary = rule_scope
            summary_code = ""
            summary_params = {}
        else:
            choices = (
                PermissionChoice("allow_once", "Allow once", "This invocation only", "No save"),
                PermissionChoice("deny", "Deny", "No execution", "No save"),
            )
            grant_summary = "This invocation only. No additional rule or directory is saved."
            summary_code = "permission.scope.invocation_only"
            summary_params = {}
        preview, http_method, http_target, command, shell_note = _display_details(
            tool_name, tool_call.arguments, cwd.value
        )
        target_display = tuple(dict.fromkeys(
            _target_text(item) for item in targets
        ))
        shown_subject = http_target or subject
        return PermissionPrompt(
            request_id=tool_call.id or f"{identity.session_id}:{identity.call_id}",
            tool_name=tool_name,
            subject=shown_subject,
            reason=reason,
            risk_flags=access.risk_flags,
            targets=target_display,
            choices=choices,
            principal=identity.agent_task_id or identity.session_id,
            operation=", ".join(sorted(access.operations)),
            grant_summary=grant_summary,
            preview=preview,
            cwd=cwd.value,
            command=command,
            http_method=http_method,
            http_target=http_target,
            shell_note=shell_note,
            reason_code=reason_code,
            reason_params=_pairs(reason_params or {}),
            summary_code=summary_code,
            summary_params=_pairs(summary_params),
        )

    def _allowed(
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
        remember_rule: PermissionRule | None = None,
        choice: str = "allow_once",
        reason_code: str = "",
        reason_params: dict[str, str] | None = None,
    ) -> PermissionResolution:
        session_dirs: list[ExecutionPath] = []
        persistent_dirs: list[ExecutionPath] = []
        session_rules: list[dict[str, object]] = []
        persistent_rules: list[dict[str, object]] = []
        if choice in {"allow_session_directory", "allow_persistent_directory"}:
            for target in targets:
                if target.path is None or target.classification != PathClass.OUTSIDE:
                    continue
                directory = target.path.parent if target.declaration.kind == "file" else target.path
                if directory not in session_dirs:
                    session_dirs.append(directory)
                if choice == "allow_persistent_directory" and directory not in persistent_dirs:
                    persistent_dirs.append(directory)
        if remember_rule is not None and not remember_rule.unresolved:
            record = remember_rule.to_persistent()
            if choice == "allow_session_rule":
                session_rules.append(record)
            elif choice == "allow_persistent_rule":
                session_rules.append(record)
                persistent_rules.append(record)
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
            _blocked_paths(backend, cwd),
        )
        return PermissionResolution(
            arguments,
            "allow",
            reason,
            access.risk_flags,
            source,
            grant,
            AuthorizationChange(
                tuple(session_dirs),
                tuple(persistent_dirs),
                tuple(session_rules),
                tuple(persistent_rules),
            ),
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

    if any(target.kind == "directory" for target in access.targets):
        return None
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
        and item.classification in {PathClass.IN_ORIGIN, PathClass.IN_GRANTED}
    ]
    paths = {item.path.value for item in file_targets if item.path is not None}
    if len(paths) == 1:
        item = file_targets[0]
        assert item.path is not None
        root = _containing_root(item.path.value, scope)
        relative = relative_to_root(item.path.value, root) if root else None
        if root and relative and ".." not in PurePosixPath(relative).parts:
            parent = PurePosixPath(relative).parent.as_posix()
            pattern = PurePosixPath(relative).name if parent == "." else f"{parent}/*"
            operations = tuple(sorted({
                target.declaration.operation for target in file_targets
            }))
            return PermissionRule.file_grant(tool_name, root, pattern, operations)

    url_value = arguments.get("url")
    if isinstance(url_value, str) and url_value and "network_read" in access.operations:
        origin = parse_http_origin(url_value)
        method = str(arguments.get("method", "GET")).upper()
        if origin is not None and method in {"GET", "HEAD", "OPTIONS"}:
            scheme, host, port = origin
            return PermissionRule.network_grant(tool_name, scheme, host, port, (method,))
    return None


def _containing_root(path: str, scope: AccessScope) -> str | None:
    roots = [str(Path(scope.origin).resolve())]
    roots.extend(str(Path(item).resolve()) for item in scope.additional)
    containing = [root for root in roots if path == root or is_under(path, root)]
    if not containing:
        return None
    return max(containing, key=len)


def _outside_directories(targets: tuple[ResolvedTarget, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for item in targets:
        if item.path is None or item.classification != PathClass.OUTSIDE:
            continue
        directory = item.path.parent if item.declaration.kind == "file" else item.path
        if directory.value not in found:
            found.append(directory.value)
    return tuple(found)


def _directory_grant_text(directories: tuple[str, ...]) -> str:
    shown = "; ".join(directories) or "(no directory)"
    return (
        f"Adds file root {shown}. File tools may use this directory for the "
        "chosen lifetime. This does not grant shell, network, or every operation."
    )


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
    method = arguments.get("method", "GET")
    return MatchContext(
        session_origin=str(Path(scope.origin).resolve()),
        files=files,
        url=url,
        method=str(method).upper() if isinstance(method, str) else "GET",
    )


_PREVIEW_LIMIT = 800


def _display_details(
    tool_name: str,
    arguments: dict,
    cwd: str,
) -> tuple[str, str, str, str, str]:
    preview = ""
    http_method = ""
    http_target = ""
    command = ""
    shell_note = ""
    if tool_name in {"write_file", "edit_file"}:
        if "content" in arguments:
            preview = _bound_preview(str(arguments.get("content", "")))
        elif "old_text" in arguments or "new_text" in arguments:
            preview = _bound_preview(
                "replace:\n"
                + str(arguments.get("old_text", ""))
                + "\nwith:\n"
                + str(arguments.get("new_text", ""))
            )
    if tool_name in {"http_request", "web_fetch"}:
        http_method = str(arguments.get("method", "GET")).upper()
        raw_url = arguments.get("url")
        http_target = redact_http_target(raw_url) if isinstance(raw_url, str) else ""
        header_count = len(arguments.get("headers") or {}) if isinstance(arguments.get("headers"), dict) else 0
        if header_count:
            http_target = f"{http_target} (headers redacted: {header_count})"
        if arguments.get("body") not in (None, "", {}):
            http_target = f"{http_target} (body redacted)"
    if tool_name == "execute_command":
        raw_command = arguments.get("command")
        command = raw_command if isinstance(raw_command, str) else ""
        shell_note = (
            f"Approves this whole command in a local process at {cwd}. "
            "File grants and protected-path checks do not confine that process. "
            "There is no OS sandbox."
        )
    return preview, http_method, http_target, command, shell_note


def _target_text(item: ResolvedTarget) -> str:
    if item.path is not None:
        return item.path.value
    if item.declaration.kind == "url":
        return redact_http_target(str(item.declaration.value))
    return str(item.declaration.value)


def _bound_preview(text: str) -> str:
    if len(text) <= _PREVIEW_LIMIT:
        return text
    return text[:_PREVIEW_LIMIT] + "\n… preview truncated"


def _permission_shape(access: ToolAccess) -> tuple:
    return (
        access.operations,
        tuple((target.kind, target.operation, target.value, target.recursive) for target in access.targets),
        access.subject,
    )


def _blocked_paths(
    backend: PathResolver | None,
    cwd: ExecutionPath,
) -> tuple[ExecutionPath, ...]:
    if backend is None:
        return ()
    paths: list[ExecutionPath] = []
    for forbidden in forbidden_paths():
        try:
            path = backend.resolve_path(str(forbidden), cwd=cwd)
        except Exception:
            continue
        if path not in paths:
            paths.append(path)
    return tuple(paths)
