"""权限层的对外门面。

把分散在 types/resolver/config/interactive 四个子模块里的公共符号统一在这里 re-export,
让包外只需记一个入口:`from ..permission import X`(包内仍按子模块精确 import)。
这样"某个符号住在哪个文件"是包的内部细节,外部不必关心,搬动子模块也不波及调用方。

子模块职责一眼看清:
- types      判定结果的数据形状(PermissionCheckResult)
- resolver   编排 + 通用策略 + 责任链(Policy / Request / Resolver / Fallback)
- config     规则式 handler:按持久化配置自动裁决
- interactive 交互式 handler:把 ask 抛给终端前的人

注意 re-export 顺序:types 必须最先,后面几个模块都依赖它。
"""

from .types import PermissionCheckResult, PermissionDecision
from .resolver import (
    FallbackApprovalHandler,
    PermissionApprovalHandler,
    UserInteractionHandler,
    PermissionPolicy,
    PermissionRequest,
    PermissionResolver,
)
from .config import (
    PermissionMode,
    PermissionRule,
    PermissionSettings,
    RuleBasedApprovalHandler,
    append_allow_rule,
    default_settings_path,
    load_permission_settings,
)
from .interactive import InteractiveApprovalHandler

__all__ = [
    "PermissionCheckResult",
    "PermissionDecision",
    "PermissionApprovalHandler",
    "UserInteractionHandler",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionResolver",
    "FallbackApprovalHandler",
    "PermissionMode",
    "PermissionRule",
    "PermissionSettings",
    "RuleBasedApprovalHandler",
    "append_allow_rule",
    "default_settings_path",
    "load_permission_settings",
    "InteractiveApprovalHandler",
]
