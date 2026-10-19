"""Normalize explicit resource grants at the application authorization boundary."""
from dataclasses import replace

from ...domain.policy.permission.scope import path_module
from ...domain.policy.permission.settings import PermissionRule, relative_to_root
from ...domain.policy.permission.types import AuthorizationChange


class GrantError(ValueError):
    pass

def resource_change(session, backend, snapshot, resource: dict, lifetime: str) -> AuthorizationChange:
    """Normalize with the backend; reject raw tool and wildcard grants from UI."""
    kind, path = resource.get("kind"), resource.get("path")
    if kind not in {"file", "directory"} or not isinstance(path, str) or not path.strip():
        raise GrantError("Choose a file or directory and enter its path")
    if lifetime not in {"session", "project", "user"}:
        raise GrantError("Choose a supported authorization lifetime")
    operations = resource.get("operations")
    if operations not in (["file_read"], ["file_read", "file_write"]):
        raise GrantError("Choose read-only or read and write access")
    resolved = backend.resolve_path(path)
    if snapshot.scope.classify(resolved.value).value == "forbidden":
        raise GrantError("Protected permission resources cannot be authorized")
    root = resolved.value if kind == "directory" else resolved.parent.value
    pattern = "" if kind == "directory" else path_module(resolved.value).basename(resolved.value)
    rule = PermissionRule("*", kind=kind, root=root, pattern=pattern, operations=tuple(operations),
                          exact=kind == "file", recursive=kind == "directory")
    relative = relative_to_root(resolved.value, str(session.workspace_dir))
    if lifetime in {"session", "project"} and (relative is not None or resolved.value == str(session.workspace_dir)):
        rule = replace(rule, root="", root_kind="workspace", pattern=relative or "")
    record = replace(rule, lifetime=lifetime)
    return AuthorizationChange(session_rules=(record,) if lifetime == "session" else (),
                               project_rules=(record,) if lifetime == "project" else (),
                               persistent_rules=(record,) if lifetime == "user" else ())
