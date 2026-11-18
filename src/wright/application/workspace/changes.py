"""Git review scopes and version-checked changes in one execution directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ...core.git import git_command
from ...domain.model.workspace.project import ProjectContext
from .change_lock import change_operation, workspace_change_lock

MAX_PATCH_BYTES = 1024 * 1024
SCOPES = {"working", "unstaged", "staged"}


class DiffError(ValueError):
    pass


class ChangeConflict(DiffError):
    pass


def _run(
    root: Path,
    *args: str,
    data: bytes | None = None,
    allowed=(0,),
    index_file: Path | None = None,
) -> bytes:
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        environment.pop(key, None)
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file.resolve())
    result = subprocess.run(
        git_command(root, *args, flags=("--literal-pathspecs",)),
        input=data,
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode not in allowed:
        raise DiffError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or "Git operation failed"
        )
    return result.stdout


def _scope(value: str) -> str:
    if value not in SCOPES:
        raise DiffError("unknown change scope")
    return value


def _is_git(root: Path) -> bool:
    if not root.is_dir():
        raise DiffError("the execution directory is no longer available")
    return (
        _run(root, "rev-parse", "--is-inside-work-tree", allowed=(0, 128)).strip()
        == b"true"
    )


def _head(root: Path) -> str:
    return (
        _run(root, "rev-parse", "--verify", "HEAD", allowed=(0, 128)).decode().strip()
    )


def _base(root: Path) -> str:
    return (
        _head(root)
        or _run(root, "hash-object", "-t", "tree", "--stdin", data=b"").decode().strip()
    )


def _safe(root: Path, path: str) -> Path:
    value = PurePosixPath(path)
    if (
        not path
        or value.is_absolute()
        or ".." in value.parts
        or "\0" in path
        or path == "."
    ):
        raise DiffError("invalid change path")
    target = root.joinpath(*value.parts)
    try:
        target.parent.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise DiffError("change path escapes execution root") from exc
    if target.is_symlink():
        raise DiffError("change symlink is unsafe or escapes execution root")
    return target


def _status_rows(root: Path) -> list[dict[str, Any]]:
    prefix = _run(root, "rev-parse", "--show-prefix").decode().strip()
    parts = (
        _run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        .decode("utf-8", "surrogateescape")
        .split("\0")
    )
    rows = []
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        code, path = item[:2], item[3:]
        old = None
        if code[0] in "RC" and index < len(parts):
            old = parts[index]
            index += 1
        if prefix:
            if not path.startswith(prefix) or (
                old is not None and not old.startswith(prefix)
            ):
                continue
            path = path[len(prefix) :]
            old = old[len(prefix) :] if old else None
        rows.append(
            {
                "path": path,
                "old_path": old,
                "status": code.strip(),
                "staged": code[0] not in " ?",
                "unstaged": code[1] != " ",
                "untracked": code == "??",
                "conflicted": "U" in code or code in {"AA", "DD"},
            }
        )
    return rows


def _diff_args(root: Path, scope: str, base: str | None = None) -> list[str]:
    args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--relative",
        "--find-renames",
    ]
    if scope == "staged":
        args += ["--cached", base or _base(root)]
    elif scope == "working":
        args += [base or _base(root)]
    return args


def _diff_rows(root: Path, scope: str, base: str | None = None) -> list[dict[str, Any]]:
    parts = (
        _run(root, *_diff_args(root, scope, base), "--name-status", "-z", "--")
        .decode("utf-8", "surrogateescape")
        .split("\0")
    )
    rows = []
    index = 0
    while index < len(parts) and parts[index]:
        code, path = parts[index : index + 2]
        index += 2
        old = None
        if code[0] in "RC":
            old, path = path, parts[index]
            index += 1
        rows.append({"path": path, "old_path": old, "status": code})
    return rows


def _paths(row: dict) -> list[str]:
    return list(
        dict.fromkeys(path for path in (row.get("old_path"), row["path"]) if path)
    )


def _fingerprint(root: Path, rows: list[dict]) -> str:
    digest = hashlib.sha256()
    digest.update(_head(root).encode())
    digest.update(_run(root, "ls-files", "--stage", "-z"))
    for path in sorted({path for row in rows for path in _paths(row)}):
        digest.update(path.encode("utf-8", "surrogateescape"))
        target = root / path
        try:
            info = target.lstat()
            digest.update(f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:".encode())
            if stat.S_ISREG(info.st_mode):
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif target.is_symlink():
                digest.update(os.readlink(target).encode("utf-8", "surrogateescape"))
        except FileNotFoundError:
            digest.update(b"missing")
    return digest.hexdigest()


@dataclass
class _Batch:
    """Git facts shared by every row of one listing.

    A listing used to start several Git processes per file; with dozens of
    changed files that took seconds. Values missing here fall back to asking
    Git about the one row, so a batch only ever saves work.
    """

    prefix: str
    base: str
    sizes: dict[str, int] = field(default_factory=dict)
    diffs: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path, rows: list[dict] = (), scope: str = "working") -> _Batch:
        prefix = _run(root, "rev-parse", "--show-prefix").decode().strip()
        batch = cls(prefix, _base(root))
        batch.sizes = _object_sizes(
            root, [name for row in rows for name in batch.object_names(row, scope)]
        )
        return batch

    def object_names(self, row: dict, scope: str) -> list[str]:
        if row.get("untracked"):
            return []
        path = row["path"]
        names = []
        if scope != "unstaged":
            names.append(f"{self.base}:{self.prefix}{row.get('old_path') or path}")
        if scope in {"staged", "unstaged"}:
            names.append(f":{self.prefix}{path}")
        return names


def _object_sizes(root: Path, names: list[str]) -> dict[str, int]:
    unique = list(dict.fromkeys(names))
    if not unique:
        return {}
    lines: list[str] | None = None
    # One name per line, so a name with a newline cannot share the batch.
    if not any("\n" in name for name in unique):
        data = "".join(f"{name}\n" for name in unique).encode("utf-8", "surrogateescape")
        lines = (
            _run(root, "cat-file", "--batch-check=%(objectsize)", data=data)
            .decode("utf-8", "surrogateescape")
            .splitlines()
        )
        if len(lines) != len(unique):
            lines = None
    if lines is None:
        lines = [
            _run(root, "cat-file", "-s", name, allowed=(0, 128)).decode(
                "utf-8", "surrogateescape"
            )
            for name in unique
        ]
    # A missing object prints "<name> missing" instead of a size.
    return {
        name: int(line.strip())
        for name, line in zip(unique, lines, strict=True)
        if line.strip().isdigit()
    }


def _measure(root: Path, row: dict, scope: str, batch: _Batch) -> tuple[Path, int, bool, bool]:
    path = row["path"]
    target = _safe(root, path)
    if row.get("old_path"):
        _safe(root, row["old_path"])
    size = target.stat().st_size if scope != "staged" and target.is_file() else 0
    binary = False
    if scope != "staged" and target.is_file():
        with target.open("rb") as handle:
            binary = b"\0" in handle.read(8192)
    object_size = max(
        (batch.sizes.get(name, 0) for name in batch.object_names(row, scope)), default=0
    )
    if scope == "staged":
        size = object_size
    return target, size, binary, max(size, object_size) > MAX_PATCH_BYTES


_C_ESCAPES = {ord(k): v for k, v in zip("abtnvfr\"\\", b"\a\b\t\n\v\f\r\"\\", strict=True)}


def _unquote(value: bytes) -> tuple[bytes, bytes] | None:
    """Read one C-quoted Git path; return it and what follows."""
    if not value.startswith(b'"'):
        return None
    out = bytearray()
    index = 1
    while index < len(value):
        char = value[index]
        if char == ord('"'):
            return bytes(out), value[index + 1 :]
        if char == ord("\\") and index + 1 < len(value):
            following = value[index + 1]
            if following in _C_ESCAPES:
                out.append(_C_ESCAPES[following])
                index += 2
                continue
            octal = value[index + 1 : index + 4]
            if re.fullmatch(rb"[0-7]{3}", octal):
                out.append(int(octal, 8))
                index += 4
                continue
            return None
        out.append(char)
        index += 1
    return None


def _section_path(section: bytes) -> str | None:
    lines = section.split(b"\n")
    for line in lines[1:]:
        if line.startswith((b"@@", b"--- ", b"Binary files ", b"GIT binary patch")):
            break
        for marker in (b"rename to ", b"copy to "):
            if line.startswith(marker):
                name = line[len(marker) :]
                quoted = _unquote(name)
                return (quoted[0] if quoted else name).decode("utf-8", "surrogateescape")
    rest = lines[0][len(b"diff --git ") :]
    if rest.startswith(b'"'):
        first = _unquote(rest)
        if first is None or not first[1].startswith(b" "):
            return None
        rest = first[1][1:]
        second = _unquote(rest)
        name = second[0] if second else rest
    elif rest.endswith(b'"'):
        second = _unquote(rest[rest.rfind(b' "b/') + 1 :])
        if second is None:
            return None
        name = second[0]
    else:
        # Unquoted and not a rename: "a/<path> b/<path>" with the same path.
        half = len(rest) // 2
        if len(rest) % 2 == 0 or rest[half : half + 1] != b" " or rest[2:half] != rest[half + 3 :]:
            return None
        name = rest[half + 1 :]
    if not name.startswith(b"b/"):
        return None
    return name[2:].decode("utf-8", "surrogateescape")


def _combined_diffs(root: Path, scope: str, base: str, rows: list[dict]) -> dict[str, bytes]:
    """One Git diff for many rows, split back into each row's section."""
    diffs: dict[str, bytes] = {}
    for start in range(0, len(rows), 200):
        chunk = rows[start : start + 200]
        paths = list(dict.fromkeys(path for row in chunk for path in _paths(row)))
        try:
            raw = _run(
                root,
                *_diff_args(root, scope, base),
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--",
                *paths,
            )
        except DiffError:
            continue
        sections = [part for part in re.split(rb"(?m)^(?=diff --git )", raw) if part]
        found = {}
        for section in sections:
            path = _section_path(section)
            if path is None or path in found:
                found = None
                break
            found[path] = section
        if found is None:
            continue  # unreadable output: every row in the chunk asks Git itself
        # Every section was matched, so a row without one has an empty diff.
        diffs.update({row["path"]: found.get(row["path"], b"") for row in chunk})
    return diffs


def _patch_data(root: Path, row: dict, scope: str, batch: _Batch | None = None) -> dict:
    path = row["path"]
    batch = batch or _Batch.load(root, [row], scope)
    target, size, binary, truncated = _measure(root, row, scope, batch)
    raw = b""
    if row.get("untracked"):
        if target.is_file() and not binary and not truncated:
            raw = _run(
                root,
                "diff",
                "--no-index",
                "--no-ext-diff",
                "--no-color",
                "--",
                "/dev/null",
                path,
                allowed=(0, 1),
            )
    elif not truncated:
        raw = batch.diffs.get(path)
        if raw is None:
            raw = _run(root, *_diff_args(root, scope, batch.base), "--", *_paths(row))
    binary = binary or any(
        line == b"GIT binary patch"
        or (line.startswith(b"Binary files ") and line.endswith(b" differ"))
        for line in raw.splitlines()
    )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        binary = True
        decoded = ""
    truncated = truncated or len(raw) > MAX_PATCH_BYTES
    patch = "" if binary or truncated else decoded
    additions = deletions = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@ "):
            in_hunk = True
        elif in_hunk:
            additions += line.startswith("+")
            deletions += line.startswith("-")
    return {
        "patch": patch,
        "binary": binary,
        "truncated": truncated,
        "size": size,
        "additions": additions,
        "deletions": deletions,
    }


def list_changes(context: ProjectContext, scope: str = "working") -> dict[str, Any]:
    scope = _scope(scope)
    root = context.execution_root
    if not _is_git(root):
        return {
            "scope": scope,
            "environment": context.environment,
            "baseline": "",
            "local_warning": False,
            "changes": [],
            "git": False,
            "revision": "",
        }
    status = _status_rows(root)
    base = _base(root)
    rows = _diff_rows(root, scope, base)
    status_map = {row["path"]: row for row in status}
    if scope != "staged":
        rows += [dict(row) for row in status if row["untracked"]]
    if scope == "working":
        seen = {row["path"] for row in rows}
        rows += [dict(row) for row in status if row["path"] not in seen]
    merged = [
        {**status_map.get(original["path"], {}), **original}
        for original in sorted(rows, key=lambda item: item["path"])
    ]
    batch = _Batch.load(root, merged, scope)
    diffable = []
    for row in merged:
        try:
            if not row.get("untracked") and not _measure(root, row, scope, batch)[3]:
                diffable.append(row)
        except (DiffError, OSError):
            pass  # reported for the row below
    batch.diffs = _combined_diffs(root, scope, batch.base, diffable)
    changes = []
    for row in merged:
        try:
            metadata = _patch_data(root, row, scope, batch)
            error = ""
        except (DiffError, OSError) as exc:
            metadata = {
                "size": 0,
                "patch": "",
                "binary": False,
                "truncated": False,
                "additions": 0,
                "deletions": 0,
            }
            error = str(exc)
        patch = metadata.pop("patch")
        target = root / row["path"]
        if not error and target.is_file() and target.stat().st_nlink > 1:
            error = "Hard-linked files do not support safe changes from this view."
        if not error and target.is_dir():
            error = "Submodules and directories require review in their own workspace."
        special = row.get("conflicted") or error
        can_hunk = bool(
            patch
            and "@@ " in patch
            and not row.get("old_path")
            and not special
            and not metadata["binary"]
            and not metadata["truncated"]
        )
        row.update(metadata)
        row["reason"] = error or (
            "Resolve merge conflicts before changing this file."
            if row.get("conflicted")
            else ""
        )
        row["capabilities"] = {
            "stage": scope != "staged" and not bool(special),
            "unstage": scope != "unstaged"
            and bool(row.get("staged"))
            and not bool(special),
            "discard": scope != "staged"
            and not bool(special)
            and row["size"] <= MAX_PATCH_BYTES,
            "hunk": can_hunk
            and scope in {"unstaged", "staged"}
            and not row.get("untracked"),
        }
        changes.append(row)
    revision = hashlib.sha256(
        (scope + _fingerprint(root, status) + json.dumps(rows, sort_keys=True)).encode(
            "utf-8", "surrogateescape"
        )
    ).hexdigest()
    return {
        "scope": scope,
        "environment": context.environment,
        "baseline": "index" if scope == "unstaged" else "HEAD",
        "local_warning": context.environment == "local",
        "git": True,
        "changes": changes,
        "revision": revision,
    }


def change_patch(
    context: ProjectContext, path: str, scope: str = "working"
) -> dict[str, Any]:
    _safe(context.execution_root, path)
    view = list_changes(context, scope)
    row = next((row for row in view["changes"] if row["path"] == path), None)
    if row is None:
        raise DiffError("path is not an active change in this scope")
    data = _patch_data(context.execution_root, row, scope)
    if list_changes(context, scope)["revision"] != view["revision"]:
        raise ChangeConflict("files changed while loading the diff; refresh and retry")
    return {**row, **data, "revision": view["revision"], "scope": scope}


def _hunk_patch(patch: str, index: int) -> bytes:
    lines = patch.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^@@ -\d+", line)]
    if index < 0 or index >= len(starts):
        raise DiffError("this diff hunk is no longer available")
    end = starts[index + 1] if index + 1 < len(starts) else len(lines)
    header = [
        line
        for line in lines[: starts[0]]
        if not line.startswith(("new file mode ", "deleted file mode "))
    ]
    return "".join([*header, *lines[starts[index] : end]]).encode("utf-8")


def _index_path(root: Path) -> Path:
    value = _run(root, "rev-parse", "--git-path", "index").decode().strip()
    path = Path(value)
    return path if path.is_absolute() else root / path


def _write_atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.wright-change-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        if mode is not None:
            temporary.chmod(stat.S_IMODE(mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _index_transaction(root: Path, *, promote: bool = True, reserve: bool = False):
    """Stage edits in a private index; never roll back another Git writer.

    Git commands acquire a lock for the private index themselves. Promotion
    takes Git's real index.lock and compares the original index again. Commit
    additionally reserves the real index before it can update HEAD.
    """
    index = _index_path(root)
    index.parent.mkdir(parents=True, exist_ok=True)
    original = index.read_bytes() if index.exists() else None
    lock = index.with_name(index.name + ".lock")
    descriptor = None
    temporary = None

    def reserve_index():
        try:
            return os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ChangeConflict(
                "another Git process is updating the index; refresh and retry"
            ) from exc

    try:
        if reserve:
            descriptor = reserve_index()
            if (index.read_bytes() if index.exists() else None) != original:
                raise ChangeConflict("the Git index changed; refresh and retry")
        fd, name = tempfile.mkstemp(prefix="wright-review-index-", dir=index.parent)
        os.close(fd)
        temporary = Path(name)
        if original is None:
            temporary.unlink()
        else:
            temporary.write_bytes(original)

        def run(*args: str, **kwargs):
            return _run(root, *args, index_file=temporary, **kwargs)

        yield run
        if promote:
            if descriptor is None:
                descriptor = reserve_index()
            if (index.read_bytes() if index.exists() else None) != original:
                raise ChangeConflict(
                    "the Git index changed during this operation; refresh and retry"
                )
            if temporary.exists():
                os.replace(temporary, index)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.name + ".lock").unlink(missing_ok=True)
        if descriptor is not None:
            os.close(descriptor)
            lock.unlink(missing_ok=True)


def apply_action(
    context: ProjectContext,
    *,
    action: str,
    scope: str = "working",
    paths: list[str],
    expected_revision: str,
    hunk: int | None = None,
    message: str = "",
    confirm: bool = False,
) -> dict:
    scope = _scope(scope)
    root = context.execution_root
    if action not in {"stage", "unstage", "discard", "commit"}:
        raise DiffError("unknown change action")
    if not expected_revision:
        raise ChangeConflict("refresh changes before applying an action")
    if action in {"discard", "commit"} and not confirm:
        raise DiffError("confirmation is required")
    with workspace_change_lock(root):
        view = list_changes(context, scope)
        if not view["git"]:
            raise DiffError("Git actions require a Git repository")
        if view["revision"] != expected_revision:
            raise ChangeConflict(
                "changes have changed since you reviewed them; refresh and retry"
            )
        if action == "commit":
            if scope != "staged" or not message.strip() or "\0" in message:
                raise DiffError("enter a commit message in the staged view")
            if not view["changes"]:
                raise DiffError("there are no staged changes to commit")
            if any(row.get("conflicted") for row in _status_rows(root)):
                raise DiffError("resolve merge conflicts before committing")
            _check_commit_scope(root)
            with _index_transaction(root, reserve=True) as run:
                if list_changes(context, scope)["revision"] != expected_revision:
                    raise ChangeConflict(
                        "the staged changes changed; refresh and retry"
                    )
                _check_commit_scope(root)
                run("commit", "-m", message.strip())
            return {"applied": True, "commit": _head(root), "results": []}
        unique = list(dict.fromkeys(paths))
        if not unique:
            raise DiffError("select at least one file")
        by_path = {row["path"]: row for row in view["changes"]}
        rows = []
        for path in unique:
            row = by_path.get(path)
            if row is None or not row["capabilities"].get(action):
                raise DiffError(
                    f"{path}: this action is unavailable in the selected scope"
                )
            for related in _paths(row):
                target = _safe(root, related)
                if target.exists() and not target.is_file():
                    raise DiffError(f"{related}: only regular files can be changed")
                if target.is_file() and target.stat().st_nlink > 1:
                    raise DiffError(
                        f"{related}: hard-linked files cannot be changed safely"
                    )
            rows.append(row)
        if hunk is not None and (
            len(rows) != 1
            or not rows[0]["capabilities"]["hunk"]
            or (action == "stage" and scope != "unstaged")
            or (action == "unstage" and scope != "staged")
            or (action == "discard" and scope != "unstaged")
        ):
            raise DiffError("select one supported hunk in its staged or unstaged scope")
        all_paths = list(dict.fromkeys(path for row in rows for path in _paths(row)))
        backups: dict[str, tuple[bytes, int] | None] = {}
        written: dict[str, tuple[bytes, int] | None] = {}
        if action == "discard":
            for path in all_paths:
                target = _safe(root, path)
                if target.is_file() and target.stat().st_size > MAX_PATCH_BYTES:
                    raise DiffError(f"{path}: file is too large to discard safely")
                backups[path] = (
                    (target.read_bytes(), target.stat().st_mode)
                    if target.is_file()
                    else None
                )
        try:
            with _index_transaction(
                root, promote=action != "discard" or scope == "working"
            ) as run:
                if list_changes(context, scope)["revision"] != expected_revision:
                    raise ChangeConflict(
                        "files changed before this action; refresh and retry"
                    )
                if action == "discard":
                    _discard_selected(root, scope, rows, run, backups, written, hunk)
                else:
                    _apply_selected(
                        root,
                        context,
                        run,
                        rows,
                        all_paths,
                        action,
                        scope,
                        hunk,
                        expected_revision,
                    )
                    if list_changes(context, scope)["revision"] != expected_revision:
                        raise ChangeConflict(
                            "files changed during this operation; refresh and retry"
                        )
        except Exception:
            if action == "discard":
                for path, applied in reversed(list(written.items())):
                    target = _safe(root, path)
                    # A writer that changed our result owns those newer bytes.
                    if _disk_state(target) != applied:
                        continue
                    original = backups[path]
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        _write_atomic(target, *original)
            raise
        return {
            "applied": True,
            "results": [{"path": path, "result": "applied"} for path in unique],
        }


def _apply_selected(
    root, context, run, rows, all_paths, action, scope, hunk, expected_revision
):
    if hunk is not None:
        data = _hunk_patch(_patch_data(root, rows[0], scope)["patch"], hunk)
        flags = ["--cached"]
        if action == "unstage":
            flags.append("--reverse")
        run("apply", "--check", *flags, "--whitespace=nowarn", data=data)
        if list_changes(context, scope)["revision"] != expected_revision:
            raise ChangeConflict("files changed before this action; refresh and retry")
        run("apply", *flags, "--whitespace=nowarn", data=data)
    elif action == "stage":
        run("add", "-A", "--", *all_paths)
    elif action == "unstage":
        if _head(root):
            run("restore", "--staged", "--source=HEAD", "--", *all_paths)
        else:
            run("rm", "--cached", "-r", "--ignore-unmatch", "--", *all_paths)


def _disk_state(target: Path) -> tuple[bytes, int] | None:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ChangeConflict("a selected path changed its file type; refresh and retry")
    return (target.read_bytes(), target.stat().st_mode) if target.is_file() else None


def _source_state(root: Path, scope: str, path: str) -> tuple[bytes, int] | None:
    if scope == "working":
        raw = _run(root, "ls-tree", "-z", _base(root), "--", path)
    else:
        raw = _run(root, "ls-files", "--stage", "-z", "--", path)
    if not raw:
        return None
    parts = raw.split(b"\t", 1)[0].split()
    mode, oid = parts[0], parts[2] if scope == "working" else parts[1]
    if mode not in {b"100644", b"100755"}:
        raise DiffError(f"{path}: restoring this file type is unsupported")
    if int(_run(root, "cat-file", "-s", oid.decode())) > MAX_PATCH_BYTES:
        raise DiffError(f"{path}: stored file is too large to discard safely")
    return (_run(root, "cat-file", "blob", oid.decode()), stat.S_IFREG | int(mode, 8))


def _discard_selected(root, scope, rows, run, backups, written, hunk):
    desired = {}
    for path, original in backups.items():
        if _disk_state(_safe(root, path)) != original:
            raise ChangeConflict("a selected file changed; refresh and retry")
        desired[path] = _source_state(root, scope, path)
    if hunk is not None:
        path = rows[0]["path"]
        original = backups[path]
        if original is None:
            raise DiffError("a deleted file requires a whole-file action")
        # Apply the server hunk to a private copy first. Git never partially
        # modifies the real working tree during hunk validation.
        with tempfile.TemporaryDirectory(prefix="wright-discard-") as temporary:
            scratch = Path(temporary)
            target = scratch / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original[0])
            data = _hunk_patch(_patch_data(root, rows[0], scope)["patch"], hunk)
            _run(scratch, "apply", "--reverse", "--whitespace=nowarn", data=data)
            desired[path] = (target.read_bytes(), original[1])
    if scope == "working":
        tracked = list(
            dict.fromkeys(
                path for row in rows if not row["untracked"] for path in _paths(row)
            )
        )
        if tracked:
            run("restore", "--source=" + _base(root), "--staged", "--", *tracked)
    for path, result in desired.items():
        target = _safe(root, path)
        if _disk_state(target) != backups[path]:
            raise ChangeConflict(
                "a selected file changed before discard; refresh and retry"
            )
        if result is None:
            target.unlink(missing_ok=True)
        else:
            _write_atomic(target, *result)
        written[path] = _disk_state(target)


def git_action(service: Any, **body: Any) -> dict:
    with change_operation(service):
        return apply_action(service.runtime.project_context, **body)


def _check_commit_scope(root: Path) -> None:
    prefix = (
        _run(root, "rev-parse", "--show-prefix")
        .decode("utf-8", "surrogateescape")
        .strip()
    )
    if not prefix:
        return
    paths = _run(
        root,
        "diff",
        "--cached",
        "--no-relative",
        "--no-renames",
        "--name-only",
        "-z",
        _base(root),
        "--",
    )
    if any(
        path and not path.startswith(prefix.encode("utf-8", "surrogateescape"))
        for path in paths.split(b"\0")
    ):
        raise DiffError(
            "staged changes outside this workspace are not shown; review and commit from the repository root"
        )
