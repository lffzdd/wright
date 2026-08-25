from dataclasses import dataclass
from typing import Literal


PermissionDecision = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionCheckResult:
    decision: PermissionDecision
    reason: str
    risk_flags: tuple[str, ...] = ()
    # 权限层可以返回一份经用户/审批器确认后的参数,例如未来 sed 预览确认后注入
    # 已审核的新内容。None 表示沿用模型原始 arguments。
    updated_arguments: dict | None = None
    # 记录判定来源:policy / tool / user / hook / approval_handler / error...
    source: str = "policy"
