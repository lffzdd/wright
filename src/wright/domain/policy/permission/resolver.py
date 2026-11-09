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
from .scope import AccessScope, PathClass, is_under, path_module
from .settings import (
    MatchContext,
    PermissionConfiguration,
    PermissionRule,
    PermissionSettings,
    command_dialect,
    command_segments,
    direct_command_argv,
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

_Outcome = tuple[PermissionDecision, str, str, str, dict[str, str]]
_NETWORK = frozenset({"network_read", "network_write"})


@dataclass(frozen=True)
class _Call:
    """One invocation as the policy stages see it."""

    access: ToolAccess  # as the tool declared it
    effective: ToolAccess  # after capability grants (sandbox network) are applied
    tool_name: str
    subject: str
    in_scope: bool
    context: MatchContext | None
    session_rules: tuple[PermissionRule, ...]
    settings: PermissionConfiguration | None
    ceiling: str | None  # "ask" or "plan": operations above it are denied
    mode: str | None  # fallback for calls no rule decides; None under a plan ceiling

    @property
    def execution_mode(self) -> str:
        return self.context.execution_mode if self.context is not None else ""

    @property
    def requests_sandbox_network(self) -> bool:
        operations = self.access.operations
        return "shell" in operations and bool(operations & _NETWORK) and self.execution_mode == "sandbox"


def _ceiling(call: _Call) -> _Outcome | None:
    """Ask and Plan are hard ceilings; no later stage can widen them."""

    if call.ceiling is None:
        return None
    limit = ASK_OPERATIONS if call.ceiling == "ask" else PLAN_OPERATIONS
    operations = call.access.operations
    if "unknown" not in operations and operations <= limit:
        return None
    if call.ceiling == "ask":
        return "deny", "Ask mode blocks this operation", "mode", "permission.ask_denied", {}
    return "deny", "Plan mode blocks this operation", "mode", "permission.plan_denied", {}


def _restrictions(call: _Call) -> _Outcome | None:
    """Configured deny, then ask, rules."""

    settings = call.settings
    if settings is None:
        return None
    params = {"tool_name": call.tool_name, "subject": call.subject}
    if _matches(settings.deny, call.tool_name, call.subject, effect="deny", context=call.context):
        return ("deny", f"Matched a deny rule: {call.tool_name}({call.subject})",
                "rule_config", "permission.deny_rule", params)
    if _matches(settings.ask, call.tool_name, call.subject, effect="ask", context=call.context):
        return ("ask", f"Matched an ask rule and needs confirmation: {call.tool_name}({call.subject})",
                "rule_config", "permission.ask_rule", params)
    return None


def _git_control(call: _Call) -> _Outcome | None:
    # Git runs what these files name. Neither a rule nor a mode may write them
    # silently, or a later git call becomes arbitrary code.
    if "file_write" in call.access.operations and GIT_CONTROL_FLAG in call.access.risk_flags:
        return "ask", "Writing Git configuration or hooks needs approval for this invocation", "policy", "permission.git_control", {}
    return None


def _sandbox_network(call: _Call) -> _Outcome | None:
    # A remembered sandbox-network grant is applied when the call is built
    # (see ``effective``); without one, network use needs this approval.
    if call.requests_sandbox_network and call.effective.operations & _NETWORK:
        return "ask", "This command requests network access", "policy", "permission.network_requested", {}
    return None


def _unknown_effects(call: _Call) -> _Outcome | None:
    if call.effective.operations & {"external_unknown", "unknown"}:
        return "ask", "Unknown or undeclared external effects need approval for this invocation", "policy", "permission.unknown_operation", {}
    return None


def _allow_rules(call: _Call) -> _Outcome | None:
    """Remembered and configured grants; every lifetime is one rule set here."""

    context = call.context
    files = context.files if context is not None else ()
    rules = tuple(
        rule for rule in (*call.session_rules, *(call.settings.allow if call.settings else ()))
        if rule.kind != "sandbox_network" and (call.in_scope or rule.kind != "tool" or not files)
    )
    contexts = tuple(replace(context, files=(file,)) for file in files) if files else (context,)
    allowed = all(_matches(rules, call.tool_name, call.subject, effect="allow", context=item) for item in contexts)
    if files and not call.effective.operations <= {operation for _, operation in files}:
        # The call does more than touch these files; only a tool grant covers that.
        tools = [rule for rule in rules if rule.kind == "tool"]
        allowed = _matches(tools, call.tool_name, call.subject, effect="allow", context=context)
    if not allowed:
        return None
    return ("allow", f"Matched an allow rule: {call.tool_name}({call.subject})",
            "rule_config", "permission.allow_rule", {"tool_name": call.tool_name, "subject": call.subject})


# What each mode allows inside the session scope when no rule decided:
# (modes, operations the call must stay within, reason, display code).
_MODE_FALLBACK = (
    (frozenset({"acceptEdits", "bypass"}), frozenset({"file_read", "file_write"}),
     "Workspace editing is enabled", "permission.accept_edits"),
    # Durable writes (schedules, long-term memory) outlive this boundary, so
    # persistent_write is absent.
    (frozenset({"bypass"}), frozenset({"shell", "internal_read", "execution_control", "plan_update", "user_interaction"}),
     "Sandbox command approval is disabled", "permission.bypass_allow"),
)


def _mode_fallback(call: _Call) -> _Outcome | None:
    if call.mode is None or not call.in_scope:
        return None
    # Modes remove confirmations inside a boundary; a host Shell has none.
    if "shell" in call.effective.operations and call.execution_mode == "host":
        return None
    for modes, operations, reason, code in _MODE_FALLBACK:
        if call.mode in modes and call.effective.operations <= operations:
            return "allow", reason, "mode", code, {}
    return None


_DEFAULT_ALLOW = frozenset({"file_read", "internal_read", "plan_update"})


def _default(call: _Call) -> _Outcome:
    operations = call.effective.operations
    if not call.in_scope and operations & {"file_read", "file_write"}:
        return "ask", "The target is outside this session's access scope", "scope", "permission.outside_scope", {}
    if call.in_scope and operations <= _DEFAULT_ALLOW:
        return "allow", "The default policy allows in-scope reads and internal queries", "default", "permission.default_allow", {}
    return "ask", "This operation needs approval", "policy", "permission.needs_approval", {}


# Stages that only narrow come first, so nothing after them can widen a deny
# or an ask; the first stage with an answer decides.
_STAGES = (_ceiling, _restrictions, _git_control, _sandbox_network, _unknown_effects,
           _allow_rules, _mode_fallback)


class PermissionPolicy:
    """Hard policy layer; tool names and arguments remain outside this class."""

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
    ) -> _Outcome:
        """Return decision, English reason, source, display code, and display params."""

        call = self._call(access, tool_name=tool_name, subject=subject, in_scope=in_scope, context=context,
                          session_rules=session_rules, interaction_mode=interaction_mode, permission_mode=permission_mode)
        for stage in _STAGES:
            outcome = stage(call)
            if outcome is not None:
                return outcome
        return _default(call)

    def _call(self, access, *, tool_name, subject, in_scope, context, session_rules, interaction_mode, permission_mode) -> _Call:
        settings = self.settings
        mode = permission_mode or (settings.mode if settings is not None else "default")
        ceiling = "ask" if interaction_mode == "ask" else "plan" if "plan" in {interaction_mode, mode} else None
        call = _Call(access, access, tool_name, subject, in_scope, context, session_rules, settings, ceiling,
                     None if "plan" in {interaction_mode, mode} else mode)
        if call.requests_sandbox_network and sandbox_network_granted(session_rules, settings, execution_mode="sandbox"):
            # The grant covers the network part only; the command itself is
            # still judged as an offline Shell call.
            call = replace(call, effective=replace(access, operations=access.operations - _NETWORK))
        return call

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
    executable: str = ""
    executable_target: str = ""
    command_rule: bool = False
    # Programs of a compound command that still need a rule, one per program.
    missing_command_rules: tuple[PermissionRule, ...] = ()

class PermissionResolver:
    """Pure resource preparation and invocation authority construction."""

    def __init__(self, policy: PermissionPolicy | None = None, *, execution_mode: str = "sandbox"):
        if execution_mode not in {"sandbox", "host"}:
            raise ValueError("execution_mode must be sandbox or host")
        self.execution_mode = execution_mode
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

        # Reading needs any approved root; writing needs a writable one. A
        # read-only grant therefore never removes write access found elsewhere.
        in_scope = all(
            item.classification in {None, PathClass.IN_ORIGIN, PathClass.IN_GRANTED}
            and (item.path is None or item.declaration.operation != "file_write" or scope.can_write(item.path.value))
            for item in resolved
        )
        effective_access = access
        if any(item.path is not None and item.declaration.operation == "file_write" and is_git_control(item.path.value)
               for item in resolved) and GIT_CONTROL_FLAG not in access.risk_flags:
            effective_access = replace(effective_access, risk_flags=(*effective_access.risk_flags, GIT_CONTROL_FLAG))
        if not in_scope and "path_outside_scope" not in access.risk_flags:
            effective_access = replace(
                effective_access, risk_flags=(*effective_access.risk_flags, "path_outside_scope")
            )
        access_subject = access.subject or _subject_from_arguments(arguments)
        if effective_access.subject != access_subject:
            effective_access = replace(effective_access, subject=access_subject)
        sandbox_key = hashlib.sha256(json.dumps([self.execution_mode, *[str(path) for path in (*scope.roots, "writable", *scope.writable_roots, *scope.protected)]]).encode()).hexdigest()
        argv = direct_command_argv(access_subject, dialect=command_dialect(fixed_cwd.value)) if "shell" in access.operations else None
        resolve_executable = getattr(backend, "resolve_executable", None)
        executable = resolve_executable(argv[0], cwd=fixed_cwd) if argv and callable(resolve_executable) else None
        executable_target = backend.resolve_path(executable, cwd=fixed_cwd).value if executable else ""
        context = replace(_match_context(arguments, resolved, scope), cwd=fixed_cwd.value, sandbox_key=sandbox_key,
                          operations=tuple(sorted(access.operations)), executable=executable or "", executable_target=executable_target, execution_mode=self.execution_mode)
        all_rules = (*session_rules, *(self.policy.settings.allow if self.policy.settings else ()))
        missing_command_rules: tuple[PermissionRule, ...] = ()
        applied_rules = session_rules
        if "shell" in access.operations and argv is None:
            analyzed = command_segments(
                access_subject, dialect=command_dialect(fixed_cwd.value)
            )
            segments: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
            missing: list[tuple[str, str, tuple[str, ...]]] = []
            if analyzed:
                segments = _resolved_segments(access_subject, backend, fixed_cwd)
                if not segments:
                    return self._deny(
                        arguments,
                        "The shell command includes a program that could not be resolved",
                        source="resolver",
                        reason_code="permission.unresolved_command",
                    )
                missing = [item for item in segments if not any(
                    rule.tool_name == subject.name and rule.covers(item[0], item[1], item[2], self.execution_mode)
                    for rule in all_rules)]
                if segments and not missing:
                    # Deny and ask rules still run first; this only answers the allow step.
                    applied_rules = (*session_rules, PermissionRule(subject.name, kind="shell", pattern=access_subject, exact=True, cwd=fixed_cwd.value, sandbox_key=sandbox_key, execution_mode=self.execution_mode))
            missing_command_rules = tuple({item[0]: PermissionRule(
                subject.name, kind="command", argv=(item[0],), command_match="executable", cwd=fixed_cwd.value, sandbox_key=sandbox_key,
                execution_mode=self.execution_mode, operations=("shell",), executable_target=item[1]) for item in missing}.values())
        decision, reason, source, reason_code, reason_params = self.policy.apply(
            effective_access,
            tool_name=subject.name,
            subject=access_subject,
            in_scope=in_scope,
            context=context,
            session_rules=applied_rules,
            interaction_mode=interaction_mode,
            permission_mode=permission_mode,
        )

        remember_rule = _rememberable_rule(
            subject.name, arguments, effective_access, resolved, scope
        )
        if "shell" in effective_access.operations and not effective_access.operations & {"network_read", "network_write"}:
            remember_rule = PermissionRule(subject.name, kind="shell", pattern=access_subject, exact=True, cwd=fixed_cwd.value, sandbox_key=sandbox_key, execution_mode=self.execution_mode)

        if subject.requires_user_interaction and decision != "deny":
            decision = "ask"
            if not reason:
                reason = "This action needs the user"
                reason_code = "permission.user_interaction"
                reason_params = {}
            source = "user_interaction"

        return PreparedPermission(arguments, effective_access, resolved, fixed_cwd, identity, scope, remember_rule, decision, reason, source, reason_code, _pairs(reason_params), executable or "", executable_target, any(rule.kind == "command" and rule.matches(subject.name, access_subject, context=context) for rule in all_rules), missing_command_rules)

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
        command_argv: tuple[str, ...] = (),
        executable_target: str = "",
        tool_name: str = "",
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
            tuple(backend.resolve_path(str(root), cwd=cwd) for root in scope.writable_roots) if backend else (),
            network_enabled,
            execution_mode=self.execution_mode,
            command_argv=command_argv,
            executable_target=executable_target,
            network_targets=tuple(NetworkTarget(str(item.declaration.value), item.declaration.http_method or "GET", item.declaration.operation) for item in targets if item.declaration.kind == "url"),
            denied_rules=tuple(rule for rule in (self.policy.settings.deny if self.policy.settings else ())
                               if rule.kind in {"file", "directory"} and rule.tool_name in {"*", tool_name}),
            origin=str(scope.origin),
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


def sandbox_network_granted(
    session_rules: tuple[PermissionRule, ...] = (),
    settings: PermissionSettings | PermissionConfiguration | None = None,
    *,
    execution_mode: str = "",
) -> bool:
    """True when a sandbox-network capability grant is on file for this execution mode."""
    if execution_mode != "sandbox":
        return False
    allow = settings.allow if settings is not None else ()
    return any(
        rule.kind == "sandbox_network" and rule.execution_mode == "sandbox"
        for rule in (*session_rules, *allow)
    )


GIT_CONTROL_FLAG = "writes_git_control"
_GIT_CONTROL_NAMES = frozenset({"config", "config.worktree", "hooks"})


def is_git_control(path: str) -> bool:
    """Whether writing ``path`` changes what Git executes (config, hooks, gitdir link)."""
    parts = [part.casefold() for part in path_module(path).normpath(path).replace("\\", "/").split("/")]
    for index, part in enumerate(parts):
        if part != ".git":
            continue
        rest = parts[index + 1:]
        if not rest or rest[0] in _GIT_CONTROL_NAMES or "hooks" in rest or rest[-1] == "config.worktree":
            return True
        # Submodule gitdirs live under .git/modules/<name>/ (names may hold
        # slashes); Git reads their config whenever it enters the submodule.
        if rest[0] == "modules" and rest[-1] == "config":
            return True
    return False


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


_UNREMEMBERED_OPERATIONS = frozenset({
    "shell",
    "network_write",
    "external_unknown",
    "execution_control",
    "persistent_write",
})
_UNREMEMBERED_FLAGS = frozenset({
    GIT_CONTROL_FLAG,
    "executes_shell",
    "deletes_files",
    "modifies_git_state",
    "mutates_remote_state",
    "network_fetch",
    "package_manager",
})


def rememberable_access(access: ToolAccess) -> bool:
    """Whether an approval of this access may be saved as a resource grant."""
    return not (access.operations & _UNREMEMBERED_OPERATIONS or _UNREMEMBERED_FLAGS & set(access.risk_flags))


def _rememberable_rule(
    tool_name: str,
    arguments: dict,
    access: ToolAccess,
    resolved: tuple[ResolvedTarget, ...],
    scope: AccessScope,
) -> PermissionRule | None:
    """Return a bounded rule. The pattern is built from the canonical path."""

    if tool_name in {"web_search", "knowledge_search"} and access.operations == frozenset({"network_read"}):
        return PermissionRule(tool_name, kind="tool", operations=("network_read",))
    if not rememberable_access(access):
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


def _resolved_segments(command: str, backend, cwd: ExecutionPath) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """(executable, target, arguments) per program; empty when any is unknown."""
    segments = command_segments(command, dialect=command_dialect(cwd.value))
    resolve_executable = getattr(backend, "resolve_executable", None)
    if not segments or not callable(resolve_executable):
        return ()
    resolved = []
    for argv in segments:
        executable = resolve_executable(argv[0], cwd=cwd)
        if not executable:
            return ()
        resolved.append((executable, backend.resolve_path(executable, cwd=cwd).value, argv[1:]))
    return tuple(resolved)


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
