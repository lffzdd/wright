"""Public permission subsystem API."""

from .config import (
    PermissionMode,
    PermissionRule,
    PermissionSettings,
    append_additional_directory,
    append_allow_rule,
    default_settings_path,
    load_permission_settings,
)
from .interactive import InteractiveApprovalHandler
from .resolver import (
    FallbackApprovalHandler,
    PermissionApprovalHandler,
    PermissionPolicy,
    PermissionRequest,
    PermissionResolver,
    UserInteractionHandler,
)
from .scope import AccessScope, PathClass, forbidden_paths, is_under, resolve_root
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
    PermissionResponse,
    ToolAccess,
)

__all__ = [
    "AccessScope",
    "AccessTarget",
    "AuthorizationChange",
    "FallbackApprovalHandler",
    "GrantTarget",
    "InteractiveApprovalHandler",
    "InvocationGrant",
    "InvocationIdentity",
    "PathClass",
    "PermissionApprovalHandler",
    "PermissionChoice",
    "PermissionDecision",
    "PermissionMode",
    "PermissionPolicy",
    "PermissionPrompt",
    "PermissionRequest",
    "PermissionResolution",
    "PermissionResolver",
    "PermissionResponse",
    "PermissionRule",
    "PermissionSettings",
    "ToolAccess",
    "UserInteractionHandler",
    "append_additional_directory",
    "append_allow_rule",
    "default_settings_path",
    "forbidden_paths",
    "is_under",
    "load_permission_settings",
    "resolve_root",
]
