"""Project and user skill rules.

Skills are the stored rules the agent catalog can load. Writes go through
the skill store, which checks the id and refuses paths outside the directory.
A model tool is not given this writer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ...core.paths import (
    project_id,
    project_skills_dir,
    skill_directories,
    user_skills_dir,
)
from ...domain.model.skills import SkillNotFoundError, SkillStoreError
from ...infrastructure.storage.skills import (
    load_skill_file,
    normalize_skill_id,
    scan_skills,
    skill_file_path,
    write_skill,
)

Scope = Literal["project", "user"]


class RuleError(ValueError):
    pass


def list_rules(project_root: Path) -> dict[str, Any]:
    directories = skill_directories(project_root)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scope, directory in (("project", directories[0]), ("user", directories[1])):
        definitions, errors = scan_skills(directory)
        for definition in definitions:
            identifier = definition.meta.id
            rows.append({
                **_public(definition),
                "scope": scope,
                "shadowed": identifier in seen,
            })
            seen.add(identifier)
        for error in errors:
            rows.append({"scope": scope, "error": error})
    return {
        "project_id": project_id(project_root),
        "rules": rows,
        "note": "allowed-tools is a suggestion. It does not grant permission.",
    }


def get_rule(project_root: Path, skill_id: str) -> dict[str, Any]:
    definition, scope = _load(project_root, skill_id)
    return {**_public(definition), "scope": scope, "body": definition.body}


def save_rule(
    project_root: Path,
    skill_id: str,
    *,
    scope: Scope,
    description: str,
    body: str,
    allowed_tools: list[str] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    directory = _directory(project_root, scope)
    try:
        normalized = normalize_skill_id(skill_id)
        path = skill_file_path(directory, normalized)
    except SkillStoreError as exc:
        raise RuleError(str(exc)) from exc
    if path.is_file() and not confirm:
        raise RuleError("confirmation is required to replace an existing rule")
    try:
        written = write_skill(
            directory,
            normalized,
            name=normalized,
            description=description,
            body=body,
            allowed_tools=allowed_tools,
        )
        definition = load_skill_file(written, normalized)
    except SkillStoreError as exc:
        raise RuleError(str(exc)) from exc
    return {**_public(definition), "scope": scope, "body": definition.body}


def delete_rule(project_root: Path, skill_id: str, *, scope: Scope, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise RuleError("confirmation is required")
    directory = _directory(project_root, scope)
    try:
        normalized = normalize_skill_id(skill_id)
        path = skill_file_path(directory, normalized)
    except SkillStoreError as exc:
        raise RuleError(str(exc)) from exc
    if path.is_symlink() or path.parent.is_symlink():
        raise RuleError("refusing to delete a symlinked rule")
    if not path.is_file():
        raise RuleError("rule was not found")
    path.unlink()
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
    return {"id": normalized, "scope": scope, "deleted": True}


def _directory(project_root: Path, scope: str) -> Path:
    if scope == "project":
        return project_skills_dir(project_root)
    if scope == "user":
        return user_skills_dir()
    raise RuleError("scope must be project or user")


def _load(project_root: Path, skill_id: str):
    last: Exception | None = None
    for scope, directory in (
        ("project", project_skills_dir(project_root)),
        ("user", user_skills_dir()),
    ):
        try:
            path = skill_file_path(directory, skill_id)
        except SkillStoreError as exc:
            raise RuleError(str(exc)) from exc
        if not path.is_file():
            continue
        try:
            return load_skill_file(path, normalize_skill_id(skill_id)), scope
        except (SkillStoreError, SkillNotFoundError) as exc:
            last = exc
    if last is not None:
        raise RuleError(str(last)) from last
    raise RuleError("rule was not found")


def _public(definition: Any) -> dict[str, Any]:
    meta = definition.meta.to_dict()
    meta.pop("path", None)
    meta.pop("skill_root", None)
    return meta


__all__ = ["RuleError", "delete_rule", "get_rule", "list_rules", "save_rule"]
