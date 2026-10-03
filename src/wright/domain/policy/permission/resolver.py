"""Single permission policy and invocation-authority resolver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ...gateway.execution import ExecutionPath, PathResolver
from ...model.tool import ToolAccess, ToolCall
from .approval import (
    PermissionApprovalHandler,
    PermissionRequest,
    ResolvedTarget,
    UserInteractionHandler,
    normalize_response,
)
from .scope import AccessScope, PathClass, is_under, path_module
from .settings import (
    MatchContext,
    PermissionRule,
    PermissionSettings,
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

    def summarize(
        self, *, roots: list[str], read_only: tuple[str, ...] = (), session_rules: tuple[PermissionRule, ...] = (),
        interaction_mode: str = "agent", permission_mode: str | None = None,
    ) -> list[dict]:
        """Describe defaults and conditional overrides without granting an invocation.

        Defaults run through the same evaluator with rules removed. Rules remain
        explicit because their result depends on the actual path or command.
        """
        settings = self.settings
        mode = permission_mode or (settings.mode if settings else "default")
        baseline = PermissionPolicy(PermissionSettings(mode=mode))
        categories = (
            ("file_read", ("read_file", "list_directory", "glob", "grep", "write_file", "edit_file")),
            ("file_write", ("write_file", "edit_file")),
            ("shell", ("execute_command",)),
        )
        groups = [
            (effect, tuple(getattr(settings, effect, ()) or ()))
            for effect in ("deny", "ask", "allow")
        ] + [("allow", session_rules)]
        result = []
        for operation, tool_names in categories:
            defaults = {}
            for scope, in_scope in (("in_scope", True), ("outside_scope", False)):
                decision, _, source, code, params = baseline.evaluate(
                    ToolAccess(frozenset({operation})), tool_name=tool_names[0],
                    subject="", in_scope=in_scope, interaction_mode=interaction_mode,
                    permission_mode=mode,
                )
                defaults[scope] = {"decision": decision, "source": source, "reason_code": code, "reason_params": params}
            rules = [
                {"effect": effect, "scope": rule.lifetime, "rule": rule.to_persistent(),
                 "target": rule.display_target(roots[0]), "resource_kind": rule.kind,
                 "operations": list(rule.operations), "tool": rule.tool_name,
                 "description": rule.describe(), "conditional": rule.kind != "tool"}
                for effect, entries in groups for rule in entries
                if (rule.tool_name in tool_names or (rule.tool_name == "*" and rule.kind in {"file", "directory"} and operation != "shell")) and (
                    rule.kind not in {"file", "directory"} or not rule.operations or operation in rule.operations
                )
            ]
            constraints = ["rule_precedence", "invocation_required"]
            if operation != "shell":
                constraints.append("protected_permission_files")
            else:
                constraints.append("shell_system_sandbox")
            if defaults["in_scope"]["decision"] == "deny":
                constraints.append("mode_ceiling")
            result.append({
                "operation": operation, "defaults": defaults,
                "directories": list(roots), "read_only": list(read_only) if operation == "file_write" else [], "rules": rules, "constraints": constraints,
                "precedence": ["protected_permission_files", "mode_ceiling", "deny", "ask", "allow", "mode_default"],
            })
        return result


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

        if "shell" in access.operations and not scope.contains(fixed_cwd.value):
            return self._deny(arguments, "Authorize the working directory before running Shell", source="scope", reason_code="permission.outside_scope")
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
                scope=scope,
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
            scope=scope,
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
        choices = [PermissionChoice("allow_once", "Allow once", "This invocation only", "Not saved")]
        if remember_rule is not None:
            operations = remember_rule.operations or tuple(sorted(access.operations))
            resource_scope = remember_rule.describe()
            if remember_rule.kind == "file":
                resource_scope = path_module(remember_rule.root).join(remember_rule.root, remember_rule.pattern)
            elif remember_rule.kind == "directory":
                resource_scope = remember_rule.resource_root(str(cwd.value))
            elif remember_rule.kind == "network":
                resource_scope = f"{', '.join(remember_rule.methods)} {remember_rule.scheme}://{remember_rule.host}:{remember_rule.port}"
            elif remember_rule.kind == "shell":
                resource_scope = f"{remember_rule.pattern} · {remember_rule.cwd}"
            for lifetime, label in (("session", "This conversation (including resume)"), ("project", "This project"), ("user", "All projects")):
                if remember_rule.kind == "shell" and lifetime != "session":
                    continue
                choices.append(PermissionChoice(f"allow_{lifetime}_rule", label, resource_scope, label,
                                                lifetime, remember_rule.kind, operations, remember_rule.to_persistent()))
            file_targets = [item for item in targets if item.path is not None]
            unique = {item.path.value for item in file_targets}
            if len(unique) == 1 and remember_rule.kind in {"file", "directory"}:
                item = file_targets[0]
                directory = item.path.parent if item.declaration.kind == "file" else item.path
                for lifetime, label in (("session", "This conversation (including resume)"), ("project", "This project"), ("user", "All projects")):
                    for access_kind, ops in (("read", ("file_read",)), ("write", ("file_read", "file_write"))):
                        if "file_write" in access.operations and access_kind == "read":
                            continue
                        choices.append(PermissionChoice(f"allow_{lifetime}_directory_{access_kind}", label,
                            directory.value, label, lifetime, "directory", ops, {"path": directory.value, "recursive": True}))
        if "shell" in access.operations:
            choices.append(PermissionChoice("allow_once_network", "Allow this command with network", "This command only; filesystem isolation remains", "Not saved"))
        choices.append(PermissionChoice("deny", "Deny", "No execution", "Not saved"))
        choices = tuple(choices)
        target_display = tuple(dict.fromkeys(_target_text(item) for item in targets))
        grant_summary = f"This invocation only: {'; '.join(target_display)}"
        summary_code = "permission.scope.invocation_only"
        summary_params = {"targets": "; ".join(target_display)}
        preview, http_method, http_target, command, shell_note, preview_truncated = _display_details(
            tool_name, tool_call.arguments, cwd.value
        )
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
            preview_truncated=preview_truncated,
            risk_level="elevated" if set(access.risk_flags) & {
                "deletes_files", "recursive_delete", "moves_files", "modifies_git_state", "mutates_remote_state",
            } else "review",
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
        scope: AccessScope,
        remember_rule: PermissionRule | None = None,
        choice: str = "allow_once",
        reason_code: str = "",
        reason_params: dict[str, str] | None = None,
    ) -> PermissionResolution:
        session_rules: list[dict[str, object]] = []
        persistent_rules: list[dict[str, object]] = []
        project_rules: list[dict[str, object]] = []
        if remember_rule is not None and choice.startswith("allow_") and choice not in {"allow_once", "allow_once_network"}:
            lifetime = choice.split("_")[1]
            rule = replace(remember_rule, lifetime=lifetime)
            if "_directory_" in choice:
                item = next(item for item in targets if item.path is not None)
                directory = item.path.parent if item.declaration.kind == "file" else item.path
                rule = PermissionRule("*", kind="directory", root=directory.value, recursive=True,
                    operations=("file_read", "file_write") if choice.endswith("write") else ("file_read",), lifetime=lifetime)
            if lifetime in {"session", "project"} and rule.kind in {"file", "directory"} and is_under(rule.root, str(scope.origin)):
                pattern = relative_to_root(rule.root, str(scope.origin)) or ""
                if rule.kind == "file":
                    pattern = f"{pattern}/{rule.pattern}" if pattern else rule.pattern
                rule = replace(rule, root="", root_kind="workspace", pattern=pattern)
            record = rule.to_persistent()
            if lifetime == "session":
                session_rules.append(record)
            elif lifetime == "project":
                project_rules.append(record)
            elif lifetime == "user":
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
            _blocked_paths(backend, cwd, scope.protected),
            tuple(backend.resolve_path(str(root), cwd=cwd) for root in scope.roots) if backend else (),
            tuple(backend.resolve_path(str(root), cwd=cwd) for root in scope.roots
                  if not any(is_under(str(root), str(readonly)) for readonly in scope.read_only)) if backend else (),
            choice == "allow_once_network",
            shell_readonly=tuple(ExecutionPath(cwd.environment_id, str(path)) for path in scope.read_only),

        )
        return PermissionResolution(
            arguments,
            "allow",
            reason,
            access.risk_flags,
            source,
            grant,
            AuthorizationChange(session_rules=tuple(session_rules), persistent_rules=tuple(persistent_rules), project_rules=tuple(project_rules)),
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
    method = next((item.declaration.http_method for item in resolved if item.declaration.http_method), arguments.get("method", "GET"))
    return MatchContext(
        session_origin=str(scope.origin),
        files=files,
        url=url,
        method=str(method).upper() if isinstance(method, str) else "GET",
    )


_PREVIEW_LIMIT = 800


def _display_details(
    tool_name: str,
    arguments: dict,
    cwd: str,
) -> tuple[str, str, str, str, str, bool]:
    preview = ""
    http_method = ""
    http_target = ""
    command = ""
    shell_note = ""
    if tool_name in {"write_file", "edit_file"}:
        if "content" in arguments:
            preview = str(arguments.get("content", ""))
        elif "old_text" in arguments or "new_text" in arguments:
            preview = (
                "replace:\n"
                + str(arguments.get("old_text", ""))
                + "\nwith:\n"
                + str(arguments.get("new_text", ""))
            )
    if tool_name in {"http_request", "web_fetch", "web_search"}:
        http_method = "POST" if tool_name == "web_search" else str(arguments.get("method", "GET")).upper()
        raw_url = "https://api.tavily.com/search" if tool_name == "web_search" else arguments.get("url")
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
            "The process and its descendants remain inside the approved filesystem OS sandbox. "
            "Network is disabled unless separately approved for this command."
        )
    return _bound_preview(preview), http_method, http_target, command, shell_note, len(preview) > _PREVIEW_LIMIT


def _target_text(item: ResolvedTarget) -> str:
    if item.path is not None:
        return item.path.value
    if item.declaration.kind == "url":
        return redact_http_target(str(item.declaration.value))
    return str(item.declaration.value)


def _bound_preview(text: str) -> str:
    if len(text) <= _PREVIEW_LIMIT:
        return text
    return text[:_PREVIEW_LIMIT]


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
        try:
            path = backend.resolve_path(str(forbidden), cwd=cwd)
        except Exception:
            continue
        if path not in paths:
            paths.append(path)
    return tuple(paths)
