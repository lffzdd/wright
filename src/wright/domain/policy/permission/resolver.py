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
    ) -> tuple[PermissionDecision, str, str]:
        """Return decision/source before an approval adapter is consulted."""

        settings = self.settings
        mode = settings.mode if settings is not None else "default"

        # Hard mode restrictions precede configurable rules.  In particular,
        # bypass and an allow rule cannot turn a plan-mode write/MCP/control
        # operation into an executable call.
        if mode == "plan" and (
            "unknown" in access.operations
            or any(
                operation not in {
                    "file_read",
                    "internal_read",
                    "plan_update",
                    "network_read",
                    "user_interaction",
                }
                for operation in access.operations
            )
        ):
            return "deny", "plan 模式禁止该操作", "mode"

        if settings is not None and _matches(settings.deny, tool_name, subject, effect="deny", context=context):
            return "deny", f"命中 deny 规则: {tool_name}({subject})", "rule_config"
        if settings is not None and _matches(settings.ask, tool_name, subject, effect="ask", context=context):
            return "ask", f"命中 ask 规则,需确认: {tool_name}({subject})", "rule_config"

        if mode == "bypass":
            return "allow", "bypass 模式放行", "mode"
        if mode == "acceptEdits" and access.operations <= self._EDIT_OPERATIONS:
            if in_scope:
                return "allow", "acceptEdits 模式放行范围内文件编辑", "mode"

        if (
            settings is not None and _matches(
                settings.allow, tool_name, subject, effect="allow", context=context
            )
        ) or _matches(session_rules, tool_name, subject, effect="allow", context=context):
            return "allow", f"命中 allow 规则: {tool_name}({subject})", "rule_config"

        if "unknown" in access.operations:
            return "ask", "未声明的工具操作需要明确审批", "policy"
        if not in_scope and any(
            operation in {"file_read", "file_write"}
            for operation in access.operations
        ):
            return "ask", "目标不在当前会话 AccessScope 内", "scope"

        if access.operations <= self._AUTO_DEFAULT and in_scope:
            return "allow", "默认策略允许范围内读取/内部查询", "default"
        return "ask", "该操作需要审批", "policy"

    def apply(
        self,
        access: ToolAccess,
        *,
        tool_name: str,
        subject: str,
        in_scope: bool,
        context: MatchContext | None = None,
        session_rules: tuple[PermissionRule, ...] = (),
    ) -> tuple[PermissionDecision, str, str]:
        return self.evaluate(
            access,
            tool_name=tool_name,
            subject=subject,
            in_scope=in_scope,
            context=context,
            session_rules=session_rules,
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
        _rewrite_depth: int = 0,
    ) -> PermissionResolution:
        arguments = dict(tool_call.arguments)
        if backend is not None:
            fixed_cwd = cwd or backend.cwd()
        elif cwd is not None:
            fixed_cwd = cwd
        else:
            return self._deny(arguments, "没有可用的固定执行环境", source="resolver")

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
            return self._deny(arguments, "无法解析工具资源", source="resolver")
        forbidden = next(
            (item for item in resolved if item.classification == PathClass.FORBIDDEN),
            None,
        )
        if forbidden is not None:
            return self._deny(
                arguments,
                "目标属于受保护权限配置，禁止直接访问",
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
        decision, reason, source = self.policy.apply(
            effective_access,
            tool_name=subject.name,
            subject=access_subject,
            in_scope=in_scope,
            context=context,
            session_rules=session_rules,
        )

        remember_rule = _rememberable_rule(
            subject.name, arguments, effective_access, resolved, scope
        )

        if subject.requires_user_interaction and decision != "deny":
            decision = "ask"
            reason = reason or "需要用户交互"
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
                arguments, "deny", reason, effective_access.risk_flags, source, prompt=prompt
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
                f"{reason}; 没有可用交互适配器",
                effective_access.risk_flags,
                "no_interaction",
                prompt=prompt,
            )
        try:
            response = normalize_response(handler(request))
        except Exception as exc:
            return PermissionResolution(
                arguments,
                "deny",
                f"权限审批适配器失败: {type(exc).__name__}: {exc}",
                effective_access.risk_flags,
                "approval_error",
                prompt=prompt,
            )
        if response.choice in {"ask", "abstain", "deny", ""}:
            return PermissionResolution(
                arguments,
                "deny",
                f"审批拒绝: {response.choice or 'empty choice'}",
                effective_access.risk_flags,
                "approval",
                prompt=prompt,
            )
        if response.choice not in {choice.id for choice in prompt.choices}:
            return PermissionResolution(
                arguments,
                "deny",
                f"审批返回了本次请求未提供的选择: {response.choice}",
                effective_access.risk_flags,
                "invalid_choice",
                prompt=prompt,
            )

        final_arguments = dict(response.updated_arguments or arguments)
        if response.updated_arguments is not None:
            try:
                updated_access = subject.describe_access(final_arguments)
            except Exception as exc:
                return self._deny(
                    final_arguments,
                    f"审批改写后的访问描述失败: {exc}",
                    source="updated_access_description_error",
                )
            validation_error = subject.validate(final_arguments)
            if validation_error is not None:
                return self._deny(
                    final_arguments,
                    f"审批改写后的参数无效: {validation_error.err}",
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
                        "审批器第二次改写了权限相关参数，已拒绝本次调用",
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
        else:
            choices = (
                PermissionChoice("allow_once", "Allow once", "This invocation only", "No save"),
                PermissionChoice("deny", "Deny", "No execution", "No save"),
            )
            grant_summary = "This invocation only. No additional rule or directory is saved."
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
        )

    @staticmethod
    def _deny(
        arguments: dict,
        reason: str,
        *,
        risk_flags: tuple[str, ...] = (),
        source: str,
    ) -> PermissionResolution:
        return PermissionResolution(arguments, "deny", reason, risk_flags, source)


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
