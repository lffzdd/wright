"""Create test grants through the same authoritative application service."""
from wright.application.permission.resources import resource_change
from wright.application.permission.service import PermissionService
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


def memory_permissions(settings, project):
    from wright.application.composition.capabilities import bind_local_runtime
    from wright.infrastructure.config.memory_permissions import (
        MemoryPermissionRepository,
    )
    return bind_local_runtime(PermissionService(MemoryPermissionRepository(settings, project)))


class TestCheckpoint:
    __test__ = False

    def __init__(self, save, session):
        self._save = save
        self._session = session

    def save(self):
        self._save(self._session)
