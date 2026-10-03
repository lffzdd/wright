"""Permission rules, resolution, and approval contracts.

Loading and saving settings files lives in infrastructure. This package owns
the values and the decisions made from them.
"""

from __future__ import annotations

from .approval import (
    FallbackApprovalHandler,
    PermissionApprovalHandler,
    PermissionRequest,
    ResolvedTarget,
    UserInteractionHandler,
    normalize_response,
)
from .resolver import PermissionPolicy, PermissionResolver
from .scope import AccessScope, PathClass, is_under, resolve_root
from .settings import PermissionMode, PermissionRule, PermissionSettings
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
    "FallbackApprovalHandler",
    "GrantTarget",
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
    "ResolvedTarget",
    "ToolAccess",
    "UserInteractionHandler",
    "is_under",
    "normalize_response",
    "resolve_root",
]
