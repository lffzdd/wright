"""Value types shared by tool declarations and permission resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from ..execution.types import ExecutionPath

PermissionDecision = Literal["allow", "deny", "ask"]
PermissionOperation = Literal[
    "file_read",
    "file_write",
    "shell",
    "network_read",
    "network_write",
    "external_unknown",
    "internal_read",
    "plan_update",
    "execution_control",
    "persistent_write",
    "user_interaction",
    "unknown",
]


@dataclass(frozen=True)
class AccessTarget:
    """A resource named by a tool before environment resolution."""

    parameter: str
    value: str
    operation: PermissionOperation
    recursive: bool = False
    kind: Literal["file", "directory", "url", "command", "other"] = "other"


@dataclass(frozen=True)
class ToolAccess:
    """Pure description of one tool call's possible effects."""

    operations: frozenset[PermissionOperation]
    targets: tuple[AccessTarget, ...] = ()
    subject: str = ""
    risk_flags: tuple[str, ...] = ()
    reason: str = ""

    @classmethod
    def unknown(cls, reason: str = "tool did not declare access") -> ToolAccess:
        return cls(frozenset({"unknown"}), reason=reason)

    @classmethod
    def internal_read(cls, *, reason: str = "internal state query") -> ToolAccess:
        return cls(frozenset({"internal_read"}), reason=reason)

    def has(self, operation: PermissionOperation) -> bool:
        return operation in self.operations


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


@dataclass(frozen=True)
class AuthorizationChange:
    """A directory/config update committed only after final allow."""

    session_directories: tuple[ExecutionPath, ...] = ()
    persistent_directories: tuple[ExecutionPath, ...] = ()
    session_rules: tuple[str, ...] = ()
    persistent_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionChoice:
    id: str
    label: str
    scope: str
    persistence: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "scope": self.scope,
            "persistence": self.persistence,
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

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "subject": self.subject,
            "reason": self.reason,
            "risk_flags": list(self.risk_flags),
            "targets": list(self.targets),
            "choices": [choice.to_dict() for choice in self.choices],
            "principal": self.principal,
        }


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "final_arguments", MappingProxyType(dict(self.final_arguments)))
