"""Tool value objects and parameter contracts (Domain Model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ...base.value_object import ValueObject

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
class ArtifactRef:
    id: str
    media_type: str
    name: str
    size: int
    run_id: str = ""
    call_id: str = ""
    storage_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "media_type": self.media_type,
            "name": self.name,
            "size": self.size,
            "run_id": self.run_id,
            "call_id": self.call_id,
            "storage_path": self.storage_path,
        }


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class ToolResult:
    ok: bool
    err: str = ""
    data: Any = None
    summary: str = ""
    content: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()

    @classmethod
    def success(
        cls, data=None, *, summary: str = "", content=(), artifacts=()
    ) -> ToolResult:
        return cls(True, "", data, summary, tuple(content), tuple(artifacts))

    @classmethod
    def fail(
        cls, err: str, data=None, *, summary: str = "", content=(), artifacts=()
    ) -> ToolResult:
        return cls(False, err, data, summary, tuple(content), tuple(artifacts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "err": self.err,
            "data": self.data,
            "summary": self.summary,
            "content": [dict(item) for item in self.content],
            "artifacts": [item.to_dict() for item in self.artifacts],
        }


@dataclass(frozen=True)
class ToolDefinition(ValueObject):
    """Immutable tool value object defining metadata and parameter schema."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    requires_user_interaction: bool = False

    def to_schema(self) -> dict[str, Any]:
        """Convert to OpenAI / function-calling schema representation."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


__all__ = [
    "AccessTarget",
    "ArtifactRef",
    "PermissionOperation",
    "ToolAccess",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
]
