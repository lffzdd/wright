"""Tool value objects and parameter contracts (Domain Model)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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
    # ``instruction`` marks content the request projection must keep.
    # The compactor reads these fields from the serialized result; it does
    # not special-case a tool name. Empty means an ordinary foldable result.
    retention: str = ""
    retention_key: str = ""

    @classmethod
    def success(
        cls,
        data=None,
        *,
        summary: str = "",
        content=(),
        artifacts=(),
        retention: str = "",
        retention_key: str = "",
    ) -> ToolResult:
        return cls(
            True, "", data, summary, tuple(content), tuple(artifacts),
            retention, retention_key,
        )

    @classmethod
    def fail(
        cls,
        err: str,
        data=None,
        *,
        summary: str = "",
        content=(),
        artifacts=(),
        retention: str = "",
        retention_key: str = "",
    ) -> ToolResult:
        return cls(
            False, err, data, summary, tuple(content), tuple(artifacts),
            retention, retention_key,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "err": self.err,
            "data": self.data,
            "summary": self.summary,
            "content": [dict(item) for item in self.content],
            "artifacts": [item.to_dict() for item in self.artifacts],
        }
        if self.retention:
            payload["retention"] = self.retention
        if self.retention_key:
            payload["retention_key"] = self.retention_key
        return payload


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


ToolExecutionStatus = Literal["succeeded", "failed", "timeout"]


@dataclass(frozen=True)
class ToolExecutionOutcome:
    call: ToolCall
    result: ToolResult
    status: ToolExecutionStatus


class ModelVisibleTool(Protocol):
    """Fields the prompt and schema encoder may read. Callables stay outside."""

    name: str
    expose_to_model: bool
    defer_to_model: bool

    def to_dict(self) -> dict[str, Any]: ...


def split_tool_catalog(tools: Sequence[ModelVisibleTool]) -> tuple[list[str], list[str]]:
    """Baseline vs deferred names, in assembly order."""

    baseline: list[str] = []
    deferred: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        if not tool.expose_to_model or tool.name in seen:
            continue
        seen.add(tool.name)
        if tool.defer_to_model:
            deferred.append(tool.name)
        else:
            baseline.append(tool.name)
    return baseline, deferred


__all__ = [
    "AccessTarget",
    "ArtifactRef",
    "ModelVisibleTool",
    "PermissionOperation",
    "ToolAccess",
    "ToolCall",
    "ToolDefinition",
    "ToolExecutionOutcome",
    "ToolExecutionStatus",
    "ToolResult",
    "split_tool_catalog",
]
