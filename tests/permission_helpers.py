"""Create test grants through the same authoritative application service."""
from wright.application.tool_execution.permissions import PermissionService
from wright.application.workspace.grants import resource_change
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.runtime import LocalExecutionBackend


def permission_directories(session):
    return PermissionService(FilePermissionRepository(session.project_root or session.workspace_dir)).snapshot(session).scope.additional


def authorize_directory(session, directory):
    service = PermissionService(FilePermissionRepository(session.project_root or session.workspace_dir))
    snapshot = service.snapshot(session)
    change = resource_change(session, LocalExecutionBackend(session.workspace_dir, session.get_cwd), snapshot,
                             {"kind": "directory", "path": str(directory), "operations": ["file_read", "file_write"]}, "session")
    service.commit(change, session, expected_version=snapshot.version)
