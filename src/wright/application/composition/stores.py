"""Concrete stores behind the ports the session and workspace services use."""

from __future__ import annotations

from pathlib import Path

from ...core.paths import attachment_dir, session_dir, task_db_path
from ...domain.gateway.skills import ISkillStore
from ...domain.model.attachment import AttachmentRecord
from ...infrastructure.persistence.autonomy_store import AutonomyStore
from ...infrastructure.persistence.session.repository import FileSessionRepository
from ...infrastructure.storage.attachments import AttachmentStore
from ...infrastructure.storage.skills import FileSkillStore
from ...infrastructure.workspace.worktrees import WorktreeManager


def open_autonomy_store(project_root: Path, session_id: str) -> AutonomyStore:
    return AutonomyStore(task_db_path(project_root), session_id=session_id, workspace_dir=project_root)


def project_checkpoints(project_root: Path) -> FileSessionRepository:
    return FileSessionRepository(session_dir(project_root))


def copy_session_attachments(
    project_root: Path, source_id: str, target_id: str,
    records: dict[str, AttachmentRecord],
) -> dict[str, AttachmentRecord]:
    """Give a forked session its own copy of the source session's images."""
    return AttachmentStore(attachment_dir(project_root), source_id).copy_to(target_id, records)


def project_worktrees(project_root: Path) -> WorktreeManager:
    return WorktreeManager(project_root)


def skill_store() -> ISkillStore:
    return FileSkillStore()
