"""Approval contract and handler composition.

The resolver consumes this module. Adapters that talk to a user interface live
with that interface and only implement ``PermissionApprovalHandler``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...infrastructure.runtime.types import ExecutionPath
from ..tool_protocol import ToolAccess, ToolCall
from .scope import AccessScope, PathClass
from .types import (
    AccessTarget,
    InvocationIdentity,
    PermissionPrompt,
    PermissionResponse,
)


class PermissionApprovalHandler(Protocol):
    def __call__(self, request: PermissionRequest) -> PermissionResponse | str: ...


UserInteractionHandler = PermissionApprovalHandler


@dataclass(frozen=True)
class ResolvedTarget:
    declaration: AccessTarget
    path: ExecutionPath | None
    classification: PathClass | None


class PermissionRequest:
    """All context an approval adapter may inspect; it cannot grant directly."""

    def __init__(
        self,
        tool_call: ToolCall,
        arguments: dict,
        access: ToolAccess,
        targets: tuple[ResolvedTarget, ...],
        cwd: ExecutionPath,
        scope: AccessScope,
        identity: InvocationIdentity,
        prompt: PermissionPrompt,
    ) -> None:
        self.tool_call = tool_call
        self.arguments = arguments
        self.access = access
        self.targets = targets
        self.cwd = cwd
        self.scope = scope
        self.identity = identity
        self.prompt = prompt

    @property
    def subject(self) -> str:
        return self.access.subject

    @property
    def risk_flags(self) -> tuple[str, ...]:
        return self.access.risk_flags


class FallbackApprovalHandler:
    """Ask each adapter until one returns a concrete choice."""

    def __init__(self, *handlers: PermissionApprovalHandler):
        self.handlers = handlers

    def __call__(self, request: PermissionRequest) -> PermissionResponse:
        for handler in self.handlers:
            response = normalize_response(handler(request))
            if response.choice not in {"ask", "abstain"}:
                return response
        return PermissionResponse("deny")


def normalize_response(value: PermissionResponse | str) -> PermissionResponse:
    if isinstance(value, PermissionResponse):
        return value
    if isinstance(value, str):
        # Adapters may still return a bare choice ID. Only fixed IDs are
        # meaningful once the response enters the resolver.
        return PermissionResponse(value)
    raise TypeError("approval adapter must return PermissionResponse or choice ID")
