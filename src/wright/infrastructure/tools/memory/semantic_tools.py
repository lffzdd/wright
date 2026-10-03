"""Model-facing semantic memory tools.

Tools parse arguments, call MemoryService, and return ToolResult. Scope checks
live in the service, not in a second copy of the rules.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ....application.memory.memory_service import MemoryService
from ....domain.feedback import system_text
from ....domain.model.memory import SEMANTIC_MEMORY_TYPES, SEMANTIC_READ_SCOPES
from ....domain.model.tool import ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from ..system_result import fail_text

_READ_SCOPES = list(SEMANTIC_READ_SCOPES)
_WRITE_SCOPES = ["project", "global"]


def _service_for(
    service: MemoryService | None,
    directory: Path | None,
) -> MemoryService:
    if service is not None:
        return service
    from ....application.memory.memory_service import MemoryService as Service
    from ...persistence.memory import EpisodeStore, SemanticMemoryStore, memory_dir

    path = (directory or memory_dir()).expanduser().resolve()
    return Service(SemanticMemoryStore(path), EpisodeStore(path))


def create_memory(
    name: str,
    description: str,
    type: str,
    content: str,
    scope: str | None = None,
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService | None = None,
    project_id: str = "",
    directory: Path | None = None,
) -> ToolResult:
    del runtime
    chosen = scope or "project"
    record, error = _service_for(service, directory).create_semantic(
        name=name,
        description=description,
        type_=type,  # type: ignore[arg-type]
        content=content,
        scope=chosen,
        project_id=project_id,
    )
    if error is not None or record is None:
        return fail_text(error or "create failed")
    return ToolResult.success(record.to_dict())


def get_memory(
    memory_id: str,
    scope: str = "applicable",
    include_evidence: bool = False,
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService | None = None,
    project_id: str = "",
    directory: Path | None = None,
) -> ToolResult:
    del runtime
    if scope not in SEMANTIC_READ_SCOPES:
        return fail_text(system_text("memory.scope_invalid", "The memory scope is invalid"))
    record, reads, error = _service_for(service, directory).get_semantic(
        memory_id,
        project_id=project_id,
        read_scope=scope,
        include_evidence=include_evidence,
    )
    if error is not None or record is None:
        return fail_text(error or "memory not found")
    payload = record.to_dict()
    if include_evidence:
        payload["evidence_reads"] = [item.to_dict() for item in reads]
    return ToolResult.success(payload)


def update_memory(
    memory_id: str,
    expected_revision: int,
    name: str | None = None,
    description: str | None = None,
    type: str | None = None,
    content: str | None = None,
    status: str | None = None,
    scope: str = "applicable",
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService | None = None,
    project_id: str = "",
    directory: Path | None = None,
) -> ToolResult:
    del runtime
    if scope not in SEMANTIC_READ_SCOPES:
        return fail_text(system_text("memory.scope_invalid", "The memory scope is invalid"))
    record, error = _service_for(service, directory).update_semantic(
        memory_id,
        expected_revision=expected_revision,
        project_id=project_id,
        read_scope=scope,
        name=name,
        description=description,
        type_=type,  # type: ignore[arg-type]
        content=content,
        status=status,
    )
    if error is not None or record is None:
        return fail_text(error or "update failed")
    return ToolResult.success(record.to_dict())


def delete_memory(
    memory_id: str,
    scope: str = "applicable",
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService | None = None,
    project_id: str = "",
    directory: Path | None = None,
) -> ToolResult:
    del runtime
    if scope not in SEMANTIC_READ_SCOPES:
        return fail_text(system_text("memory.scope_invalid", "The memory scope is invalid"))
    deleted, error = _service_for(service, directory).delete_semantic(
        memory_id,
        project_id=project_id,
        read_scope=scope,
    )
    if error is not None or deleted is None:
        return fail_text(error or "delete failed")
    return ToolResult.success({
        "message": "Memory deleted",
        "id": deleted.id,
        "name": deleted.name,
        "scope": deleted.scope,
        "status": deleted.status,
    })


def search_memory(
    query: str = "",
    type: str | None = None,
    limit: int = 20,
    scope: str = "applicable",
    include_inactive: bool = False,
    runtime: ToolRuntime | None = None,
    *,
    service: MemoryService | None = None,
    project_id: str = "",
    directory: Path | None = None,
) -> ToolResult:
    del runtime
    if scope not in SEMANTIC_READ_SCOPES:
        return fail_text(system_text("memory.scope_invalid", "The memory scope is invalid"))
    records, error = _service_for(service, directory).search_semantic(
        query,
        type_=type,  # type: ignore[arg-type]
        limit=limit,
        project_id=project_id,
        read_scope=scope,
        include_inactive=include_inactive,
    )
    if error is not None:
        return fail_text(error)
    results = [
        {
            "id": record.id,
            "name": record.name,
            "description": record.description,
            "type": record.type,
            "scope": record.scope,
            "project_id": record.project_id,
            "status": record.status,
            "updated_at": record.updated_at,
            "revision": record.revision,
        }
        for record in records
    ]
    return ToolResult.success({
        "count": len(records),
        "scope": scope,
        "memories": "\n".join(
            f"- [{record.type}/{record.scope}] {record.id}: {record.description}"
            for record in records
        ) or "(no memories)",
        "results": results,
    })


def _describe_memory_read(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("memory_id") or arguments.get("query") or ""),
        reason="read semantic memory",
    )


def _describe_memory_write(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"persistent_write"}),
        subject=str(arguments.get("memory_id") or arguments.get("name") or ""),
        risk_flags=("persistent_state",),
        reason="change cross-session semantic memory",
    )


_MEMORY_FIELDS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
    "description": {"type": "string", "maxLength": 500},
    "type": {"type": "string", "enum": list(SEMANTIC_MEMORY_TYPES)},
    "content": {"type": "string", "minLength": 1, "maxLength": 12_000},
}


def build_memory_tools(
    directory: Path | None = None,
    *,
    service: MemoryService | None = None,
    project_id: str = "",
    service_reader: Callable[[], MemoryService] | None = None,
    project_id_reader: Callable[[], str] | None = None,
) -> list[Tool]:
    """Build tools bound to one service. ``directory`` remains for older callers."""

    def resolve_service() -> MemoryService:
        if service_reader is not None:
            return service_reader()
        return _service_for(service, directory)

    def resolve_project() -> str:
        if project_id_reader is not None:
            return project_id_reader()
        return project_id

    def bind(function):
        def call(args, runtime):
            return function(
                **args,
                runtime=runtime,
                service=resolve_service(),
                project_id=resolve_project(),
            )
        return call

    create_tool = Tool(
        name="create_memory",
        description=(
            "Create one semantic memory in the current project. "
            "A matching title does not overwrite an existing memory. "
            "Pass scope=global only when the user explicitly wants it in every project. "
            "Without a project, a project write fails instead of becoming global."
        ),
        parameters={
            "type": "object",
            "properties": {
                **_MEMORY_FIELDS,
                "scope": {"type": "string", "enum": _WRITE_SCOPES},
            },
            "required": ["name", "description", "type", "content"],
            "additionalProperties": False,
        },
        call=bind(create_memory),
        access_descriptor=_describe_memory_write,
    )
    get_tool = Tool(
        name="get_memory",
        description=(
            "Read one semantic memory by stable id, including inactive records in scope. "
            "The default scope is applicable (global plus the current project). "
            "Other projects need scope=all_projects. "
            "Set include_evidence to resolve stored source locators; it does not load a session by default."
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "minLength": 1},
                "scope": {"type": "string", "enum": _READ_SCOPES},
                "include_evidence": {"type": "boolean"},
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        call=bind(get_memory),
        access_descriptor=_describe_memory_read,
        is_concurrency_safe=lambda args: True,
    )
    update_tool = Tool(
        name="update_memory",
        description=(
            "Update one semantic memory by stable id and expected_revision. "
            "Changing the title does not change the id. "
            "status=inactive deactivates without deleting; status=active reactivates. "
            "A content edit does not reactivate an inactive memory. "
            "The default scope cannot modify another project's memory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
                "status": {"type": "string", "enum": ["active", "inactive"]},
                "scope": {"type": "string", "enum": _READ_SCOPES},
                **_MEMORY_FIELDS,
            },
            "required": ["memory_id", "expected_revision"],
            "anyOf": [
                {"required": ["name"]},
                {"required": ["description"]},
                {"required": ["type"]},
                {"required": ["content"]},
                {"required": ["status"]},
            ],
            "additionalProperties": False,
        },
        call=bind(update_memory),
        access_descriptor=_describe_memory_write,
    )
    delete_tool = Tool(
        name="delete_memory",
        description=(
            "Delete a semantic memory the user explicitly asked to forget. "
            "Deactivate with update_memory status=inactive when the file should remain. "
            "The default scope cannot delete another project's memory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "minLength": 1},
                "scope": {"type": "string", "enum": _READ_SCOPES},
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        call=bind(delete_memory),
        access_descriptor=_describe_memory_write,
    )
    search_tool = Tool(
        name="search_memory",
        description=(
            "Search semantic memories inside one scope. The default is applicable: "
            "active global memories plus the current project. "
            "An unmatched query returns no rows. Call get_memory for the body."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string", "enum": list(SEMANTIC_MEMORY_TYPES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "scope": {"type": "string", "enum": _READ_SCOPES},
                "include_inactive": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        call=bind(search_memory),
        access_descriptor=_describe_memory_read,
        is_concurrency_safe=lambda args: True,
    )
    return [create_tool, get_tool, update_tool, delete_tool, search_tool]
