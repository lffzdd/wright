"""Application approval workflow; domain resolution never waits for a user."""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from ...domain.gateway.execution import ExecutionPath, PathResolver
from ...domain.model.tool import ToolAccess, ToolCall
from ...domain.policy.permission.approval import (
    PermissionApprovalHandler,
    PermissionRequest,
    ResolvedTarget,
    UserInteractionHandler,
    normalize_response,
)
from ...domain.policy.permission.resolver import (
    PermissionPolicy,
    PermissionResolver,
    _pairs,
    _permission_shape,
)
from ...domain.policy.permission.scope import (
    AccessScope,
    is_under,
    path_module,
)
from ...domain.policy.permission.settings import (
    PermissionRule,
    PermissionSettings,
    command_dialect,
    direct_command_argv,
    redact_http_target,
    relative_to_root,
)
from ...domain.policy.permission.types import (
    AuthorizationChange,
    InvocationIdentity,
    PermissionChoice,
    PermissionPrompt,
    PermissionResolution,
    PermissionSubject,
)

_PREVIEW_LIMIT = 800


class AuthorizationWorkflow:
    """Collect approval and re-evaluate rewritten calls using a pure resolver."""

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        approval_handler: PermissionApprovalHandler | None = None,
        interaction_handler: UserInteractionHandler | None = None,
        *,
        settings: PermissionSettings | None = None,
        execution_mode: str = "sandbox",
    ):
        self.policy = policy or PermissionPolicy(settings)
        if settings is not None and self.policy.settings is None:
            self.policy = PermissionPolicy(settings)
        self.approval_handler = approval_handler
        self.interaction_handler = interaction_handler

        self.resolver = PermissionResolver(self.policy, execution_mode=execution_mode)

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
        prepared = self.resolver.prepare(tool_call, subject, backend=backend, scope=scope, identity=identity, cwd=cwd, session_rules=session_rules, interaction_mode=interaction_mode, permission_mode=permission_mode)
        if isinstance(prepared, PermissionResolution):
            return prepared
        arguments = prepared.arguments
        access = subject.describe_access(arguments)
        effective_access = prepared.access
        resolved = prepared.targets
        fixed_cwd = prepared.cwd
        remember_rule = prepared.remember_rule
        access_subject = effective_access.subject
        decision, reason, source = prepared.decision, prepared.reason, prepared.source
        reason_code, reason_params = prepared.reason_code, dict(prepared.reason_params)
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
            scope=scope,
            reason_code=reason_code,
            reason_params=reason_params,
            details=subject.approval_details(arguments, fixed_cwd.value) if subject.approval_details else {},
            executable=prepared.executable,
            executable_target=prepared.executable_target,
            missing_command_rules=prepared.missing_command_rules,
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
                command_argv=self._command_argv(access_subject, prepared.executable) if prepared.command_rule else (),
                executable_target=prepared.executable_target if prepared.command_rule else "",
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

        selected = next(choice for choice in prompt.choices if choice.id == response.choice)
        command_set = response.choice.endswith("_command_set")
        selected_rule = PermissionRule.from_mapping(dict(selected.resource)) if selected.resource.get("kind") and not command_set else None
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
            selected_rule=selected_rule,
            selected_rules=prepared.missing_command_rules if command_set else (),
            command_argv=self._command_argv(access_subject, prepared.executable) if selected_rule is not None and selected_rule.kind == "command" else (),
            executable_target=prepared.executable_target if selected_rule is not None and selected_rule.kind == "command" else "",
        )
        return resolution


    def _resolve_updated(self, *args, **kwargs) -> PermissionResolution:
        # A rewritten request is deliberately resolved from scratch.  The
        # executor tracks one rewrite round and rejects a second resource
        # change rather than allowing an approval loop.
        return self.resolve(*args, **kwargs)


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
        scope: AccessScope,
        reason_code: str = "",
        reason_params: dict[str, str] | None = None,
        details: dict | None = None,
        executable: str = "",
        executable_target: str = "",
        missing_command_rules: tuple[PermissionRule, ...] = (),
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
            candidates = [("rule", remember_rule, resource_scope)]
            if remember_rule.kind == "network":
                path = urlsplit(next((str(item.declaration.value) for item in targets if item.declaration.kind == "url"), "")).path or "/"
                path_rule = replace(remember_rule, path=path, path_match="exact")
                candidates.insert(0, ("network_path", path_rule, f"{', '.join(path_rule.methods)} {path_rule.display_target(cwd.value)}"))
            if remember_rule.kind == "shell" and executable:
                argv = direct_command_argv(subject, dialect=command_dialect(cwd.value))
                if argv:
                    command_rule = PermissionRule(tool_name, kind="command", argv=(executable,), command_match="executable", cwd=cwd.value, sandbox_key=remember_rule.sandbox_key, execution_mode=self.resolver.execution_mode, operations=("shell",), executable_target=executable_target)
                    candidates.append(("command_executable", command_rule, command_rule.describe()))
                    # A small explicit read-subcommand set gives useful prefixes;
                    # interpreter flags remain covered by the executable choice.
                    if len(argv) > 1 and argv[0].split("/")[-1] == "git" and argv[1] in {"status", "diff", "log", "show", "rev-parse", "ls-files"}:
                        prefix = replace(command_rule, argv=(executable, argv[1]), command_match="argv_prefix")
                        candidates.append(("command_prefix", prefix, prefix.describe()))
            if remember_rule.kind == "shell" and len(missing_command_rules) == 1:
                only = missing_command_rules[0]
                candidates.append(("command_executable", only, only.describe()))
            elif remember_rule.kind == "shell" and missing_command_rules:
                programs = [rule.argv[0] for rule in missing_command_rules]
                for lifetime, label in (("session", "This conversation (including resume)"), ("project", "This project"), ("user", "All projects")):
                    choices.append(PermissionChoice(f"allow_{lifetime}_command_set", label, ", ".join(programs), label, lifetime, "command", ("shell",),
                        {"kind": "command", "command_match": "executable", "argv": programs[:1], "programs": programs}))
            for suffix, candidate, candidate_scope in candidates:
                for lifetime, label in (("session", "This conversation (including resume)"), ("project", "This project"), ("user", "All projects")):
                    if candidate.kind == "shell" and lifetime != "session":
                        continue
                    record = replace(candidate, lifetime=lifetime)
                    choices.append(PermissionChoice(f"allow_{lifetime}_{suffix}", label, candidate_scope, label,
                                                    lifetime, record.kind, record.operations or operations, record.to_persistent()))
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
        if "shell" in access.operations and self.resolver.execution_mode == "sandbox":
            choices.append(PermissionChoice("allow_once_network", "Allow this command with network", "This command only; filesystem isolation remains", "Not saved"))
        choices.append(PermissionChoice("deny", "Deny", "No execution", "Not saved"))
        choices = tuple(choices)
        target_display = tuple(dict.fromkeys(_target_text(item) for item in targets))
        grant_summary = f"This invocation only: {'; '.join(target_display)}"
        summary_code = "permission.scope.invocation_only"
        summary_params = {"targets": "; ".join(target_display)}
        preview, http_method, http_target, command, shell_note, preview_truncated = _display_details(
            details or {}, cwd.value
        )
        if command and self.resolver.execution_mode == "host":
            shell_note = f"Approves this whole command on the host at {cwd.value}. No OS filesystem or network isolation applies; scripts and subprocesses have the host account access."
        shown_subject = http_target or subject
        sandboxed = bool(command) and self.resolver.execution_mode == "sandbox"
        sandbox_readable = tuple(str(root) for root in scope.roots) if sandboxed else ()
        sandbox_writable = tuple(str(root) for root in scope.roots
                                 if not any(is_under(str(root), str(path)) for path in scope.read_only)) if sandboxed else ()
        if sandboxed:
            shell_note += f" Readable directories: {', '.join(sandbox_readable)}. Approval does not widen them."
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
            execution_mode=self.resolver.execution_mode,
            sandbox_readable=sandbox_readable,
            sandbox_writable=sandbox_writable,
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
        selected_rule: PermissionRule | None = None,
        selected_rules: tuple[PermissionRule, ...] = (),
        command_argv: tuple[str, ...] = (),
        executable_target: str = "",
    ) -> PermissionResolution:
        session_rules: list[PermissionRule] = []
        persistent_rules: list[PermissionRule] = []
        project_rules: list[PermissionRule] = []
        if remember_rule is not None and choice.startswith("allow_") and choice not in {"allow_once", "allow_once_network"}:
            lifetime = choice.split("_")[1]
            rule = replace(selected_rule or remember_rule, lifetime=lifetime)
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
            records = [replace(item, lifetime=lifetime) for item in selected_rules] or [rule]
            if lifetime == "session":
                session_rules.extend(records)
            elif lifetime == "project":
                project_rules.extend(records)
            elif lifetime == "user":
                persistent_rules.extend(records)
        return self.resolver.issue(arguments, access, targets, cwd, identity, reason, source, backend=backend, scope=scope,
            changes=AuthorizationChange(tuple(session_rules), tuple(persistent_rules), tuple(project_rules)), network_enabled=choice == "allow_once_network", command_argv=command_argv, executable_target=executable_target, reason_code=reason_code, reason_params=reason_params)

    @staticmethod
    def _command_argv(command: str, executable: str) -> tuple[str, ...]:
        argv = direct_command_argv(command)
        return (executable, *argv[1:]) if argv and executable else ()

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


def _display_details(details: dict, cwd: str) -> tuple[str, str, str, str, str, bool]:
    preview = str(details.get("preview", ""))
    command = str(details.get("command", ""))
    http_method = str(details.get("http_method", ""))
    url = details.get("http_target", "")
    http_target = redact_http_target(url) if url else ""
    count = details.get("header_count", 0)
    if count:
        http_target += f" (headers redacted: {count})"
    if details.get("has_body"):
        http_target += " (body redacted)"
    shell_note = (
        f"Approves this whole command in a local process at {cwd}. "
        "The process and its descendants remain inside the approved filesystem OS sandbox. "
        "Network is disabled unless separately approved for this command."
    ) if command else ""
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


