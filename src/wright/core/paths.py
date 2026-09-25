"""User-level and per-project paths.

The workspace is the project being edited (cwd or --workspace). Runtime
state — sessions, traces, the task database, memory — lives under ~/.wright
so it does not mix with source files.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def wright_home() -> Path:
    override = os.environ.get("WRIGHT_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".wright").expanduser().resolve()


def project_id(workspace: Path) -> str:
    resolved = workspace.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    slug = "".join(
        ch if ch.isalnum() else "-" for ch in resolved.name
    ).strip("-") or "workspace"
    return f"{slug}-{digest}"


def project_state_dir(workspace: Path) -> Path:
    return wright_home() / "projects" / project_id(workspace)


def session_dir(workspace: Path) -> Path:
    return project_state_dir(workspace) / "sessions"


def attachment_dir(workspace: Path) -> Path:
    """Root for session-owned binary attachments, outside edited workspaces."""
    return project_state_dir(workspace) / "attachments"


def artifact_dir(workspace: Path) -> Path:
    """Managed delivered outputs; never use the edited workspace as storage."""
    return project_state_dir(workspace) / "artifacts"


def trace_dir(workspace: Path) -> Path:
    return project_state_dir(workspace) / "traces"


def task_db_path(workspace: Path) -> Path:
    return project_state_dir(workspace) / "tasks.sqlite3"


def user_skills_dir() -> Path:
    return wright_home() / "skills"


def project_skills_dir(workspace: Path) -> Path:
    return workspace.expanduser().resolve() / ".wright" / "skills"


def skill_directories(workspace: Path) -> list[Path]:
    """Project skills override user skills when ids collide."""
    return [project_skills_dir(workspace), user_skills_dir()]


def user_permission_settings_path() -> Path:
    return wright_home() / "permission_settings.json"


def user_mcp_config_path() -> Path:
    return wright_home() / "mcp.json"


def project_mcp_config_path(workspace: Path) -> Path:
    return workspace.expanduser().resolve() / ".wright" / "mcp.json"


def mcp_config_paths(workspace: Path) -> list[Path]:
    """User config first; project `.wright/mcp.json` overrides the same server name."""
    return [user_mcp_config_path(), project_mcp_config_path(workspace)]


def ensure_project_state(workspace: Path) -> Path:
    """Create the per-project state tree; return its root."""
    root = project_state_dir(workspace)
    session_dir(workspace).mkdir(parents=True, exist_ok=True)
    attachment_dir(workspace).mkdir(parents=True, exist_ok=True)
    artifact_dir(workspace).mkdir(parents=True, exist_ok=True)
    trace_dir(workspace).mkdir(parents=True, exist_ok=True)
    return root
