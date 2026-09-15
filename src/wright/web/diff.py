"""Read-only, path-safe git changes for the local Web inspector."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from ..project import ProjectContext

MAX_PATCH_BYTES = 1024 * 1024


class DiffError(ValueError):
    pass


def _git(root: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )
    if result.returncode not in allowed:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DiffError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _safe_relative(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise DiffError("invalid change path")
    candidate = root.joinpath(*relative.parts)
    resolved_parent = candidate.parent.resolve()
    try:
        resolved_parent.relative_to(root.resolve())
    except ValueError as exc:
        raise DiffError("change path escapes execution root") from exc
    if candidate.exists() or candidate.is_symlink():
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise DiffError("change symlink escapes execution root") from exc
    return candidate


def _status_rows(raw: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    parts = raw.decode("utf-8", errors="replace").split("\0")
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        status = item[:2]
        path = item[3:]
        if status[0] in {"R", "C"} and index < len(parts):
            path = parts[index]
            index += 1
        rows.append({
            "path": path,
            "status": status.strip() or "M",
            "untracked": status == "??",
        })
    return rows


def list_changes(context: ProjectContext) -> dict[str, Any]:
    root = context.execution_root
    status = _status_rows(_git(root, "status", "--porcelain=v1", "-z"))
    if context.environment == "worktree" and context.base_commit:
        tracked_raw = _git(
            root,
            "diff",
            "--name-status",
            "-z",
            context.base_commit,
            "--",
        )
        tracked: dict[str, dict[str, Any]] = {}
        pieces = tracked_raw.decode("utf-8", errors="replace").split("\0")
        index = 0
        while index < len(pieces):
            code = pieces[index]
            index += 1
            if not code or index >= len(pieces):
                continue
            path = pieces[index]
            index += 1
            if code.startswith(("R", "C")) and index < len(pieces):
                path = pieces[index]
                index += 1
            tracked[path] = {
                "path": path,
                "status": code,
                "untracked": False,
            }
        for row in status:
            if row["untracked"]:
                tracked[row["path"]] = row
        changes = sorted(tracked.values(), key=lambda item: str(item["path"]))
        baseline = context.base_commit
    else:
        changes = sorted(status, key=lambda item: str(item["path"]))
        baseline = "HEAD"
    return {
        "environment": context.environment,
        "baseline": baseline,
        "local_warning": context.environment == "local",
        "changes": changes,
    }


def change_patch(context: ProjectContext, path: str) -> dict[str, Any]:
    root = context.execution_root
    candidate = _safe_relative(root, path)
    summary = next(
        (item for item in list_changes(context)["changes"] if item["path"] == path),
        None,
    )
    if summary is None:
        raise DiffError("path is not an active git change")
    size = candidate.stat().st_size if candidate.is_file() else 0
    binary = False
    if candidate.is_file():
        with candidate.open("rb") as handle:
            binary = b"\0" in handle.read(8_192)
    truncated = binary or size > MAX_PATCH_BYTES
    patch = ""
    if not truncated:
        if summary["untracked"]:
            output = subprocess.run(
                ["git", "diff", "--no-index", "--", "/dev/null", str(candidate)],
                check=False,
                capture_output=True,
            )
            if output.returncode not in {0, 1}:
                raise DiffError(
                    output.stderr.decode("utf-8", errors="replace").strip()
                )
            raw = output.stdout
        else:
            baseline = (
                context.base_commit
                if context.environment == "worktree" and context.base_commit
                else "HEAD"
            )
            raw = _git(root, "diff", "--no-ext-diff", baseline, "--", path)
        if len(raw) > MAX_PATCH_BYTES:
            truncated = True
        else:
            patch = raw.decode("utf-8", errors="replace")
            binary = "Binary files " in patch or "GIT binary patch" in patch
            if binary:
                patch = ""
                truncated = True
    return {
        **summary,
        "size": size,
        "binary": binary,
        "truncated": truncated,
        "patch": patch,
    }
