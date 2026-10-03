"""Value types shared by tool declarations and permission resolution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from ...gateway.execution import ExecutionPath
from ...model.tool import (  # noqa: F401
    AccessTarget,
    PermissionOperation,
    ToolAccess,
    ToolResult,
)

PermissionDecision = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionSubject:
    """The narrow tool contract required by permission resolution."""

    name: str
    requires_user_interaction: bool
    describe_access: Callable[[dict], ToolAccess]
    validate: Callable[[dict], ToolResult | None]


@dataclass(frozen=True)
class InvocationIdentity:
    session_id: str
    run_id: str = ""
    call_id: str = ""
    agent_task_id: str | None = None


@dataclass(frozen=True)
class GrantTarget:
    path: ExecutionPath
    operation: PermissionOperation
    recursive: bool = False


@dataclass(frozen=True)
class InvocationGrant:
    """Minimal immutable authority for exactly one tool invocation."""

    identity: InvocationIdentity
    environment_id: str
    cwd: ExecutionPath
    operations: frozenset[PermissionOperation]
    targets: tuple[GrantTarget, ...] = ()
    subject: str = ""
    command: str | None = None
    blocked_paths: tuple[ExecutionPath, ...] = ()
    shell_readable: tuple[ExecutionPath, ...] = ()
    shell_writable: tuple[ExecutionPath, ...] = ()
    network_enabled: bool = False
    policy_version: str = ""
    shell_readonly: tuple[ExecutionPath, ...] = ()


@dataclass(frozen=True)
class AuthorizationChange:
    """A directory/config update committed only after final allow."""

    session_rules: tuple[dict[str, object], ...] = ()
    persistent_rules: tuple[dict[str, object], ...] = ()
    project_rules: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class PermissionChoice:
    id: str
    label: str
    scope: str
    persistence: str
    lifetime: str = "once"
    resource_kind: str = "invocation"
    operations: tuple[str, ...] = ()
    resource: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "persistence": self.persistence,
            "lifetime": self.lifetime,
            "resource_kind": self.resource_kind,
            "operations": list(self.operations),
            "resource": dict(self.resource),
        }


@dataclass(frozen=True)
class PermissionPrompt:
    """Structured approval request shared by CLI, TUI and Web renderers."""

    request_id: str
    tool_name: str
    subject: str
    reason: str
    risk_flags: tuple[str, ...]
    targets: tuple[str, ...]
    choices: tuple[PermissionChoice, ...]
    principal: str = ""
    operation: str = ""
    grant_summary: str = ""
    preview: str = ""
    cwd: str = ""
    command: str = ""
    http_method: str = ""
    http_target: str = ""
    shell_note: str = ""
    reason_code: str = ""
    reason_params: tuple[tuple[str, str], ...] = ()
    summary_code: str = ""
    summary_params: tuple[tuple[str, str], ...] = ()
    preview_truncated: bool = False
    risk_level: Literal["review", "elevated"] = "review"

    def to_dict(self) -> dict[str, object]:
        payload = {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "subject": self.subject,
            "reason": self.reason,
            "risk_flags": list(self.risk_flags),
            "targets": list(self.targets),
            "choices": [choice.to_dict() for choice in self.choices],
            "principal": self.principal,
            "operation": self.operation,
            "grant_summary": self.grant_summary,
            "preview": self.preview,
            "cwd": self.cwd,
            "command": self.command,
            "http_method": self.http_method,
            "http_target": self.http_target,
            "shell_note": self.shell_note,
            "preview_truncated": self.preview_truncated,
            "risk_level": self.risk_level,
        }
        if self.reason_code:
            payload["reason_code"] = self.reason_code
            payload["reason_params"] = dict(self.reason_params)
        if self.summary_code:
            payload["summary_code"] = self.summary_code
            payload["summary_params"] = dict(self.summary_params)
        return payload


@dataclass(frozen=True)
class PermissionResponse:
    """Renderer/approval adapter response. Choice IDs are validated by Resolver."""

    choice: str
    updated_arguments: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.updated_arguments is not None and not isinstance(
            self.updated_arguments, MappingProxyType
        ):
            object.__setattr__(
                self,
                "updated_arguments",
                MappingProxyType(dict(self.updated_arguments)),
            )


@dataclass(frozen=True)
class PermissionResolution:
    final_arguments: Mapping[str, object]
    decision: PermissionDecision
    reason: str
    risk_flags: tuple[str, ...] = ()
    source: str = "policy"
    grant: InvocationGrant | None = None
    changes: AuthorizationChange = AuthorizationChange()
    prompt: PermissionPrompt | None = None
    reason_code: str = ""
    reason_params: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "final_arguments", MappingProxyType(dict(self.final_arguments)))
