"""Local runtime implementations behind the tool capability boundary."""

from __future__ import annotations

from pathlib import Path

from ...domain.model.session import Session
from ...infrastructure.config.permission_store import FilePermissionRepository
from ...infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from ...infrastructure.runtime.network import AuthorizedNetwork
from ..permission.service import PermissionService
from ..tool_execution import capabilities as _capabilities
from ..tool_execution.capabilities import CapabilityAssembly


def bind_local_runtime(permissions: PermissionService) -> PermissionService:
    permissions.execution_factory = permissions.execution_factory or AuthorizedExecution
    permissions.network_factory = permissions.network_factory or AuthorizedNetwork
    permissions.backend_factory = permissions.backend_factory or LocalExecutionBackend
    return permissions


def local_permission_service(project_root: Path) -> PermissionService:
    return bind_local_runtime(PermissionService(FilePermissionRepository(project_root)))


def assemble_tool_capabilities(
    session: Session | None,
    services,
    runtime_resources,
    *,
    workspace_dir: Path | None = None,
    permissions: PermissionService | None = None,
    **options,
) -> CapabilityAssembly:
    """Assemble with the authoritative local stores unless a service is given."""
    if permissions is None:
        root = getattr(session, "project_root", None) or workspace_dir or getattr(session, "workspace_dir", None) or Path.cwd()
        permissions = local_permission_service(Path(root).resolve())
    return _capabilities.assemble_tool_capabilities(
        session, services, runtime_resources, workspace_dir=workspace_dir,
        permissions=bind_local_runtime(permissions), **options,
    )
