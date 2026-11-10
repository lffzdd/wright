"""Application projections of permission policy for the existing interfaces."""
from ...domain.model.tool import ToolAccess
from ...domain.policy.permission.resolver import PermissionPolicy
from ...domain.policy.permission.settings import PermissionRule, PermissionSettings


def summarize_policy(
    policy: PermissionPolicy, *, roots: list[str], read_only: tuple[str, ...] = (), session_rules: tuple[PermissionRule, ...] = (),
    interaction_mode: str = "agent", permission_mode: str | None = None, execution_mode: str = "sandbox",
) -> list[dict]:
    """Describe defaults and conditional overrides without granting an invocation.

    Defaults run through the same evaluator with rules removed. Rules remain
    explicit because their result depends on the actual path or command.
    """
    settings = policy.settings
    mode = permission_mode or (settings.mode if settings else "default")
    baseline = PermissionPolicy(PermissionSettings(mode=mode))
    categories = (
        ("file_read", ("read", "ls", "glob", "grep", "write", "edit")),
        ("file_write", ("write", "edit")),
        ("shell", ("shell",)),
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
            constraints.append("shell_system_sandbox" if execution_mode == "sandbox" else "shell_host_access")
        if defaults["in_scope"]["decision"] == "deny":
            constraints.append("mode_ceiling")
        result.append({
            "operation": operation, "defaults": defaults,
            "directories": list(roots), "read_only": list(read_only) if operation == "file_write" else [], "rules": rules, "constraints": constraints,
            "precedence": ["protected_permission_files", "mode_ceiling", "deny", "ask", "allow", "mode_default"],
        })
    return result

