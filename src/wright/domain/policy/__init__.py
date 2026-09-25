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
from .approval import (
    FallbackApprovalHandler,
    PermissionApprovalHandler,
    PermissionRequest,
    UserInteractionHandler,
)
from .context_policy import ContextPolicy, TokenBudgetPolicy
from .guardrail_policy import GuardrailPolicy, SecurityPolicy
from .resolver import (
    PermissionPolicy,
    PermissionResolver,
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
    PermissionSubject,
    ToolAccess,
)

__all__ = [
    "AccessScope",
    "AccessTarget",
    "AuthorizationChange",
    "ContextPolicy",
    "FallbackApprovalHandler",
    "GrantTarget",
    "GuardrailPolicy",
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
    "PermissionSubject",
    "SecurityPolicy",
    "TokenBudgetPolicy",
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
