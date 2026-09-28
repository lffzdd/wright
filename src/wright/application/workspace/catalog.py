"""Register, select, and forget project directories.

Unregistering a project removes the registry entry only. The directory,
session checkpoints, and task database stay where they are.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...infrastructure.persistence.workspace.registry import (
    identity_for,
    load_registry,
    save_registry,
)


class WorkspaceCatalogError(ValueError):
    pass


class WorkspaceCatalog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def list_projects(self) -> dict[str, object]:
        data = load_registry(self.path)
        selected = data.get("selected_project_id")
        projects = []
        for item in data["projects"]:
            root = Path(str(item.get("root", "")))
            projects.append({
                **item,
                "exists": root.is_dir(),
                "selected": item.get("project_id") == selected,
            })
        return {"selected_project_id": selected, "projects": projects}

    def register(self, root: Path) -> dict[str, object]:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise WorkspaceCatalogError("workspace directory does not exist")
        identifier = identity_for(resolved)
        data = load_registry(self.path)
        projects = data["projects"]
        existing = next((item for item in projects if item.get("project_id") == identifier), None)
        if existing is None:
            existing = {
                "project_id": identifier,
                "name": resolved.name,
                "root": str(resolved),
                "registered_at": datetime.now(timezone.utc).isoformat(),
            }
            projects.append(existing)
        if not data.get("selected_project_id"):
            data["selected_project_id"] = identifier
        save_registry(data, self.path)
        return {**existing, "selected": data["selected_project_id"] == identifier}

    def unregister(self, project_id: str) -> dict[str, object]:
        data = load_registry(self.path)
        projects = data["projects"]
        remaining = [item for item in projects if item.get("project_id") != project_id]
        if len(remaining) == len(projects):
            raise WorkspaceCatalogError("workspace is not registered")
        data["projects"] = remaining
        if data.get("selected_project_id") == project_id:
            data["selected_project_id"] = remaining[0]["project_id"] if remaining else None
        save_registry(data, self.path)
        return {
            "project_id": project_id,
            "removed_from_registry": True,
            "deleted_files": False,
            "selected_project_id": data.get("selected_project_id"),
        }

    def select(self, project_id: str) -> dict[str, object]:
        data = load_registry(self.path)
        match = next((item for item in data["projects"] if item.get("project_id") == project_id), None)
        if match is None:
            raise WorkspaceCatalogError("workspace is not registered")
        data["selected_project_id"] = project_id
        save_registry(data, self.path)
        return {"selected_project_id": project_id, "root": match.get("root")}

    def get(self, project_id: str) -> dict[str, object]:
        data = load_registry(self.path)
        match = next((item for item in data["projects"] if item.get("project_id") == project_id), None)
        if match is None:
            raise WorkspaceCatalogError("workspace is not registered")
        return dict(match)

    def bootstrap(self, root: Path) -> dict[str, object]:
        """Register the launch root when the registry file does not exist yet."""

        from ...core.paths import workspace_registry_path

        path = self.path or workspace_registry_path()
        if not path.is_file():
            self.register(root)
        return self.list_projects()


__all__ = ["WorkspaceCatalog", "WorkspaceCatalogError"]
