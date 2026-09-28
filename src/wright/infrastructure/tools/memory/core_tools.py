"""Tools for inspecting and updating scoped core memory.

These adapters call MemoryService. They do not open store files or accept a
path. The bound project id comes from the session, not from the model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ....domain.model.tool import AccessTarget, ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from ..system_result import fail_text

if TYPE_CHECKING:
    from ....application.memory.memory_service import MemoryService


def _global_target(operation: str) -> AccessTarget:
    return AccessTarget(
        parameter="file",
        value="core_memory.json",
        operation=operation,  # type: ignore[arg-type]
        kind="file",
    )


def _project_target(project_id: str, operation: str) -> AccessTarget:
    value = (
        f"core/projects/{project_id}.json"
        if project_id
        else "core/projects/<unbound>.json"
    )
    return AccessTarget(
        parameter="file",
        value=value,
        operation=operation,  # type: ignore[arg-type]
        kind="file",
    )


def get_core_memory(
    service: MemoryService,
    runtime: ToolRuntime | None = None,
    *,
    project_id: str = "",
) -> ToolResult:
    """Read the composed view for the bound project."""
    del runtime
    try:
        mem = service.get_core_memory(project_id)
    except Exception as exc:
        return ToolResult.fail(f"Core memory read failed: {type(exc).__name__}")
    if mem is None:
        return ToolResult.fail("Core memory store is not configured")
    return ToolResult.success(mem.to_dict())


def update_core_memory(
    service: MemoryService,
    section: str,
    content: str = "",
    mode: str = "append",
    runtime: ToolRuntime | None = None,
    *,
    project_id: str = "",
) -> ToolResult:
    """Update one section and return the text the store saved, with its scope."""
    del runtime
    updated, err = service.update_core_memory(
        section,
        content,
        mode,
        project_id=project_id,
    )
    if updated is None:
        return fail_text(err or "Update failed")
    return ToolResult.success({
        "status": "updated",
        "section": updated.section,
        "scope": updated.scope,
        "project_id": updated.project_id,
        "content": updated.content,
    })


def build_core_memory_tools(
    service: MemoryService | None = None,
    *,
    service_reader: Callable[[], MemoryService] | None = None,
    project_id_reader: Callable[[], str] | None = None,
) -> list[Tool]:
    """Construct core memory tools bound to the current service and project."""
    if service_reader is None:
        if service is None:
            raise ValueError("core memory tools need a service")
        bound_service = service

        def service_reader() -> MemoryService:
            return bound_service
    if project_id_reader is None:
        def project_id_reader() -> str:
            return ""

    def _describe_read(args: dict[str, Any]) -> ToolAccess:
        del args
        project_id = project_id_reader()
        targets = [_global_target("internal_read")]
        if project_id:
            targets.append(_project_target(project_id, "internal_read"))
            subject = "read global core memory and the current project anchor"
        else:
            subject = "read global core memory without a project anchor"
        return ToolAccess(
            frozenset({"internal_read"}),
            targets=tuple(targets),
            subject=subject,
            reason="inspecting pinned core memory",
        )

    def _describe_write(args: dict[str, Any]) -> ToolAccess:
        section = str(args.get("section") or "")
        if section == "project_anchor":
            project_id = project_id_reader()
            targets = (_project_target(project_id, "persistent_write"),)
            subject = "update the current project core anchor"
        else:
            targets = (_global_target("persistent_write"),)
            subject = "update the global human profile"
        return ToolAccess(
            frozenset({"persistent_write"}),
            targets=targets,
            subject=subject,
            risk_flags=("persistent_state",),
            reason="updating correctable long-term core memory background",
        )

    def _bind_get(args: dict[str, Any], runtime: ToolRuntime | None = None) -> ToolResult:
        del args
        return get_core_memory(
            service_reader(),
            runtime=runtime,
            project_id=project_id_reader(),
        )

    def _bind_update(args: dict[str, Any], runtime: ToolRuntime | None = None) -> ToolResult:
        return update_core_memory(
            service_reader(),
            section=str(args.get("section") or ""),
            content=str(args.get("content") or ""),
            mode=str(args.get("mode") or "append"),
            runtime=runtime,
            project_id=project_id_reader(),
        )

    return [
        Tool(
            name="get_core_memory",
            description=(
                "View the current core memory. Persona is fixed configuration. "
                "Human profile is global. Project anchor belongs only to the current project "
                "and is absent when no project is bound. These notes are correctable long-term "
                "background and do not override the current request."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            call=_bind_get,
            access_descriptor=_describe_read,
            is_concurrency_safe=lambda args: True,
            defer_to_model=True,
        ),
        Tool(
            name="update_core_memory",
            description=(
                "Record correctable long-term background. This does not override the current "
                "request and is not a complete prompt-injection defense. "
                "human_profile is the user's cross-project profile and is stored globally; "
                "do not put one project's constraints there. "
                "project_anchor is stored only for the current project. "
                "With no current project, a project_anchor update fails instead of becoming global. "
                "Modes: append, replace, or clear. Empty content is rejected; use mode=clear to empty "
                "a section. Persona cannot be modified or cleared."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["human_profile", "project_anchor"],
                        "description": (
                            "human_profile: global user background. "
                            "project_anchor: long-term constraints of the current project only."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Text to append or replace. Required for append and replace. "
                            "Ignored for clear. An empty string does not clear the section."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["append", "replace", "clear"],
                        "default": "append",
                        "description": "append, replace, or clear. clear is the only way to empty a section.",
                    },
                },
                "required": ["section"],
                "additionalProperties": False,
            },
            call=_bind_update,
            access_descriptor=_describe_write,
            defer_to_model=True,
        ),
    ]


__all__ = [
    "build_core_memory_tools",
    "get_core_memory",
    "update_core_memory",
]
