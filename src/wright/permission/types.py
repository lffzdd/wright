"""Value types shared by tool declarations and permission resolution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from ..execution.types import ExecutionPath
from ..tool_protocol import (  # noqa: F401
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
