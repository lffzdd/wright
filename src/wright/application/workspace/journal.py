"""Session-owned file snapshots, review patches and conservative undo.

Git determines the working-tree view. This journal records a different thing:
bytes before and after this session wrote a file. Git cleanliness never means
that a journal-known file is absent.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from ...core.file_lock import FileLock
from ...core.git import git_command
from ...core.paths import session_dir
from ...domain.model.workspace.review import ChangeHead, disposition, preflight
from .change_lock import workspace_change_lock

MAX_BLOB_BYTES = 1_000_000
MAX_PATCH_BYTES = 1024 * 1024
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, _JournalLock] = {}


class ChangeJournalError(ValueError):
    pass


class _JournalLock:
    """Reentrant within a thread, exclusive across instances and processes."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.thread_lock = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.thread_lock.acquire()
        depth = getattr(self.local, "depth", 0)
        try:
            if depth == 0:
                self.directory.mkdir(parents=True, exist_ok=True)
                self.local.file_lock = FileLock(self.directory / "journal.lock")
                self.local.file_lock.acquire()
            self.local.depth = depth + 1
            return self
        except BaseException:
            self.thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.local.depth -= 1
        try:
            if self.local.depth == 0:
                self.local.file_lock.release()
        finally:
            self.thread_lock.release()


def _journal_lock(directory: Path) -> _JournalLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(directory), _JournalLock(directory))


class SessionChangeJournal:
    def __init__(self, session: Any) -> None:
        project = Path(getattr(session, "project_root", None) or session.workspace_dir)
        self.project_root = project.expanduser().resolve()
        self.execution_root = Path(session.workspace_dir).expanduser().resolve()
        self.session_id = str(session.session_id)
        if not self.session_id or self.session_id in {".", ".."} or Path(self.session_id).name != self.session_id or "\\" in self.session_id:
            raise ChangeJournalError("invalid session id")
        self.directory = session_dir(self.project_root) / "changes" / self.session_id
        self._lock = _journal_lock(self.directory)
        self._held: dict[str, dict[str, Any]] = {}

    def note_text(self, absolute: Path, *, before: str | None, after: str,
                  existed: bool, call_id: str, tool_name: str) -> None:
        relative = self._relative(absolute)
        if relative is None:
            return
        info = self._read_path(relative)
        actual = info.get("bytes")
        # File tools decode before text. Match the output encoding rather than
        # inventing UTF-8 bytes for a UTF-16/BOM file.
        encoding = "utf-8"
        if isinstance(actual, bytes):
            if actual.startswith((b"\xff\xfe", b"\xfe\xff")):
                encoding = "utf-16-le" if actual.startswith(b"\xff\xfe") else "utf-16-be"
                before_bytes = None if before is None else actual[:2] + before.encode(encoding)
            else:
                encoding = "utf-8-sig" if actual.startswith(b"\xef\xbb\xbf") else "utf-8"
                before_bytes = None if before is None else before.encode(encoding)
        else:
            before_bytes = None if before is None else before.encode(encoding)
        self._record_task(
            relative, kind="modify" if existed else "add", before=before_bytes,
            after=actual if isinstance(actual, bytes) else after.encode(encoding),
            call_id=call_id, tool_name=tool_name,
            before_info={"exists": existed, "mode": info.get("mode"),
                         "sha256": _sha(before_bytes), "bytes": before_bytes},
            after_info=info,
        )

    def capture(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            self.ensure_baseline()
            return self._snapshot()

    def commit_capture(self, before: dict[str, dict[str, Any]], *, call_id: str,
                       tool_name: str) -> None:
        with self._lock:
            self._apply_delta(before, self._snapshot(), call_id=call_id, tool_name=tool_name)

    def hold(self, command_id: str, before: dict[str, dict[str, Any]], *, call_id: str) -> None:
        with self._lock:
            self._held[command_id] = {"before": before, "call_id": call_id}

    def finish_held(self, command_id: str) -> None:
        with self._lock:
            held = self._held.pop(command_id, None)
        if held is not None:
            self.commit_capture(held["before"], call_id=held["call_id"], tool_name="execute_command")

    def ensure_baseline(self) -> None:
        with self._lock:
            state = self._load()
            if state.get("baseline_ready"):
                return
            state["baseline"] = {path: item["sha256"] for path, item in self._snapshot().items()}
            state["preexisting_paths"] = [path for _, path in _status_paths(self.execution_root)]
            state["baseline_ready"] = True
            state["version"] = 2
            self._save(state)

    def projection(self) -> dict[str, Any]:
        with self._lock:
            self.ensure_baseline()
            state = self._load()
            heads = state.get("heads") or {}
            baseline = state.get("baseline") or {}
            preexisting = state.get("preexisting_paths", list(baseline) if state.get("version", 1) < 2 else [])
            # Review needs actual current bytes for owned/diagnostic paths,
            # not a repeated full-repository capture on every UI refresh.
            current = {path: self._read_path(path) for path in set(heads) | set(preexisting)}
            changes: list[dict[str, Any]] = []
            for path, raw in sorted(heads.items()):
                info = current.get(path) or self._read_path(path)
                head = _head_from_dict(path, raw)
                live = disposition(head, current_sha256=info.get("sha256"), exists=bool(info.get("exists")))
                if raw.get("ownership_conflict") or (raw.get("after_mode") is not None and info.get("mode") != raw["after_mode"]):
                    live = "conflict"
                details = self._patch_details(path, raw)
                committed = self._committed(path, raw, info)
                contains_committed = bool(raw.get("contains_committed_edits") or self._crosses_commit(path, raw))
                unknown_commit_base = "commit_base" not in raw and "recorded_head" not in raw
                reason = ""
                if info.get("unsupported"):
                    reason = str(info["unsupported"])
                elif live == "conflict":
                    reason = "The file was changed outside this task; undo would overwrite later edits."
                elif live == "unsafe" or details.get("unavailable"):
                    reason = str(details.get("reason") or "Stored bytes are unavailable for safe undo.")
                elif committed:
                    reason = "This task edit is already committed; inspect it as history."
                elif contains_committed:
                    reason = "This task spans a commit; its original undo snapshot is historical."
                elif unknown_commit_base:
                    reason = "This older task has no recorded Git baseline; its historical undo cannot be verified."
                elif raw.get("before_sha256") == raw.get("after_sha256") and raw.get("before_mode") == raw.get("after_mode"):
                    reason = "This task has no remaining byte changes."
                changes.append({
                    **raw, "path": path, "state": live,
                    "current_sha256": info.get("sha256"),
                    "additions": details["additions"], "deletions": details["deletions"],
                    "binary": details["binary"], "truncated": details["truncated"],
                    "committed": committed, "history": committed or contains_committed or unknown_commit_base,
                    "can_revert": live in {"task", "accepted"} and not reason,
                    "reason": reason,
                })
            _annotate_renames(changes)
            # Keep diagnostics for older journals without counting untouched
            # preexisting/external files as session-owned changes.
            unowned = []
            for path in sorted(set(preexisting) - set(heads)):
                info = current.get(path) or self._read_path(path)
                expected = baseline.get(path)
                if expected is None and info.get("sha256") is None:
                    continue
                unowned.append({"path": path, "state": "preexisting" if expected == info.get("sha256") else "external",
                                "origin": "preexisting", "reversible": False, "can_revert": False})
            return {
                "scope": "session", "session_id": self.session_id,
                "execution_root": str(self.execution_root), "changes": changes,
                "unowned_changes": unowned, "revision": self._revision(state, current),
                "semantics": {"accept": "mark a task edit as reviewed; the bytes are already on disk",
                              "revert": "restore bytes from before this task first changed the file"},
            }

    def patch(self, path: str) -> dict[str, Any]:
        # The diff reads immutable journal blobs, so a later symlink or missing
        # worktree must disable undo without hiding the historical task patch.
        _relative_parts(path)
        with self._lock:
            view = self.projection()
            item = next((row for row in view["changes"] if row["path"] == path), None)
            if item is None:
                raise ChangeJournalError("path is not a recorded task change")
            if item.get("display_kind") == "rename":
                source = item["path"] if item["kind"] == "delete" else item["rename_with"]
                destination = item["rename_with"] if item["kind"] == "delete" else item["path"]
                heads = self._load()["heads"]
                old_mode, new_mode = heads[source].get("before_mode"), heads[destination].get("after_mode")
                modes = f"old mode {stat.S_IFREG | old_mode:06o}\nnew mode {stat.S_IFREG | new_mode:06o}\n" if old_mode is not None and new_mode is not None and old_mode != new_mode else ""
                return {**item, "scope": "session", "revision": view["revision"],
                        "old_path": source, "new_path": destination,
                        "patch": f"diff --git a/{source} b/{destination}\n" + modes + f"similarity index 100%\nrename from {source}\nrename to {destination}\n"}
            return {**item, **self._patch_details(path, self._load()["heads"][path]),
                    "scope": "session", "revision": view["revision"]}

    def accept(self, paths: list[str], *, confirm: bool, expected_revision: str | None = None) -> dict[str, Any]:
        return self._batch("accept", paths, confirm=confirm, expected_revision=expected_revision)

    def revert(self, paths: list[str], *, confirm: bool, expected_revision: str | None = None) -> dict[str, Any]:
        return self._batch("revert", paths, confirm=confirm, expected_revision=expected_revision)

    def _batch(self, action: str, paths: list[str], *, confirm: bool,
               expected_revision: str | None = None) -> dict[str, Any]:
        if not confirm:
            raise ChangeJournalError("confirmation is required")
        if not paths:
            raise ChangeJournalError("at least one path is required")
        unique = list(dict.fromkeys(paths))
        with workspace_change_lock(self.execution_root), self._lock:
            for path in unique:
                self._target(path)
            view = self.projection()
            if expected_revision is not None and expected_revision != view["revision"]:
                raise ChangeJournalError("changes are stale; refresh before undoing this task")
            rows = {item["path"]: item for item in view["changes"]}
            checked = preflight(action, [(path, rows.get(path, {}).get("state", "external")) for path in unique])
            for item in checked:
                row = rows.get(item["path"], {})
                if action == "revert" and not row.get("can_revert", False):
                    item.update(result="blocked", reason=row.get("reason", "This path is not owned by the task."))
                if row.get("rename_with") and row["rename_with"] not in unique:
                    item.update(result="blocked", reason="Undo both paths of a rename together.")
            if any(item["result"] == "blocked" for item in checked):
                return {"ok": False, "applied": False, "action": action, "results": checked}
            state = self._load()
            backups = {path: self._read_path(path) for path in unique}
            if self._revision(state, backups) != view["revision"]:
                raise ChangeJournalError("files changed during undo preflight; refresh and retry")
            # Validate every blob and target before the first write.
            undo_tokens: dict[str, tuple[Any, ...]] = {}
            if action == "revert":
                for path in unique:
                    head = state["heads"][path]
                    self._blob(head.get("before_sha256"))
                    self._blob(head.get("after_sha256"))
                    before_sha = head.get("before_sha256")
                    undo_tokens[path] = (
                        before_sha is not None,
                        before_sha,
                        (head.get("before_mode") if head.get("before_mode") is not None else backups[path].get("mode", 0o644)) if before_sha is not None else None,
                        None,
                    )
            attempted: list[str] = []
            try:
                for path in unique:
                    if action == "accept":
                        attempted.append(path)
                        self._mark_accepted(path)
                    else:
                        # Check once more at the write boundary for external
                        # editors that do not participate in Wright leases.
                        now = self._read_path(path)
                        if _file_token(now) != _file_token(backups[path]):
                            raise ChangeJournalError("file changed during undo; refresh and retry")
                        attempted.append(path)
                        self._restore_before(path)
                        if _file_token(self._read_path(path)) != undo_tokens[path]:
                            raise ChangeJournalError("file changed while undoing this task; refresh and retry")
                state = self._load()
                if action == "revert":
                    for path in unique:
                        if _file_token(self._read_path(path)) != undo_tokens[path]:
                            raise ChangeJournalError("file changed before undo finished; later edits were preserved")
                    for path in unique:
                        state.get("heads", {}).pop(path, None)
                    self._save(state)
            except Exception as exc:
                rollback_errors = []
                rollback_results: dict[str, str] = {}
                if action == "revert":
                    for path in reversed(attempted):
                        try:
                            info = backups[path]
                            now = _file_token(self._read_path(path))
                            if now == _file_token(info):
                                rollback_results[path] = "not_applied"
                                continue
                            if now != undo_tokens[path]:
                                rollback_results[path] = "rollback_conflict"
                                rollback_errors.append(f"{path}: changed outside this undo; its latest contents were preserved")
                                continue
                            self._write_bytes(self._target(path), info.get("bytes"), info.get("mode"), expected=undo_tokens[path])
                            rollback_results[path] = "rolled_back"
                        except Exception as rollback:
                            rollback_results[path] = "rollback_failed"
                            rollback_errors.append(f"{path}: {rollback}")
                return {"ok": False, "applied": False, "action": action,
                        "error": str(exc), "rollback_errors": rollback_errors,
                        "results": [{**item, "result": rollback_results.get(item["path"], "not_applied")} for item in checked]}
            return {"ok": True, "applied": True, "action": action,
                    "results": [{**item, "result": "applied"} for item in checked]}

    def _mark_accepted(self, path: str) -> None:
        state = self._load()
        state["heads"][path]["review"] = "accepted"
        self._save(state)

    def _restore_before(self, path: str) -> None:
        head = self._load()["heads"][path]
        current = self._read_path(path)
        if (current.get("unsupported")
                or current.get("sha256") != head.get("after_sha256")
                or bool(current.get("exists")) != head.get("after_exists", head.get("after_sha256") is not None)
                or (head.get("after_mode") is not None and current.get("mode") != head["after_mode"])):
            raise ChangeJournalError("file changed before undo could write it; later edits were preserved")
        self._write_bytes(self._target(path), self._blob(head.get("before_sha256")), head.get("before_mode"), expected=_file_token(current))

    def _write_bytes(self, target: Path, data: bytes | None, mode: int | None,
                     *, expected: tuple[Any, ...] | None = None) -> None:
        relative = target.relative_to(self.execution_root).as_posix()
        self._target(relative)
        if data is None:
            if expected is not None and _file_token(self._read_path(relative)) != expected:
                raise ChangeJournalError("file changed before undo could write it; later edits were preserved")
            if target.exists():
                if not target.is_file():
                    raise ChangeJournalError("directories cannot be undone as files")
                target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode is None:
            # Older journals did not record modes. Preserve the current mode
            # rather than applying mkstemp's private 0600 mode to user files.
            mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else 0o644
        descriptor, temporary = tempfile.mkstemp(prefix=".wright-undo-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            if mode is not None:
                os.chmod(temporary, mode)
            self._target(relative)
            if expected is not None and _file_token(self._read_path(relative)) != expected:
                raise ChangeJournalError("file changed before undo could write it; later edits were preserved")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _record_task(self, relative: str, *, kind: str, before: bytes | None,
                     after: bytes | None, call_id: str, tool_name: str,
                     before_info: dict[str, Any] | None = None,
                     after_info: dict[str, Any] | None = None) -> None:
        self._target(relative)
        with self._lock:
            before_sha, before_ok = self._store_blob(before)
            after_sha, after_ok = self._store_blob(after)
            old = before_info or {"exists": kind != "add", "sha256": before_sha}
            new = after_info or {"exists": kind != "delete", "sha256": after_sha}
            before_sha = old.get("sha256", before_sha)
            after_sha = new.get("sha256", after_sha)
            before_ok = before_ok and (not old.get("exists", kind != "add") or before is not None)
            after_ok = after_ok and (not new.get("exists", kind != "delete") or after is not None)
            state = self._load()
            if not state.get("baseline_ready"):
                state["baseline"] = {path: info["sha256"] for path, info in self._snapshot().items()}
                state["baseline"][relative] = before_sha
                state["baseline_ready"] = True
                state["version"] = 2
            heads = state.setdefault("heads", {})
            previous = heads.get(relative)
            contains_committed = bool(previous and (previous.get("contains_committed_edits") or self._committed(relative, previous, old)))
            first_before = previous.get("before_sha256") if previous else before_sha
            first_exists = previous.get("before_exists", first_before is not None) if previous else old.get("exists", kind != "add")
            after_exists = new.get("exists", kind != "delete")
            effective_kind = "add" if not first_exists else "delete" if not after_exists else "modify"
            heads[relative] = {
                "path": relative, "origin": "task", "kind": effective_kind, "review": "pending",
                "reversible": bool(before_ok and after_ok and (previous is None or previous.get("reversible"))),
                "before_sha256": first_before, "after_sha256": after_sha,
                "before_exists": first_exists, "after_exists": after_exists,
                "before_mode": previous.get("before_mode") if previous else old.get("mode"),
                "after_mode": new.get("mode"),
                "binary": bool(old.get("binary") or new.get("binary") or (previous or {}).get("binary")),
                "truncated": bool(old.get("truncated") or new.get("truncated") or (previous or {}).get("truncated")),
                "recorded_head": old.get("head_commit", self._git_head()),
                "commit_base": (previous or {}).get("commit_base", (previous or {}).get("recorded_head", old.get("head_commit", self._git_head()))),
                "contains_committed_edits": contains_committed,
                "ownership_conflict": bool((previous or {}).get("ownership_conflict") or (previous and previous.get("after_sha256") != before_sha)),
                "calls": [*(previous.get("calls", []) if previous else []),
                          {"call_id": call_id, "tool_name": tool_name, "after_sha256": after_sha}],
            }
            self._save(state)

    def _apply_delta(self, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
                     *, call_id: str, tool_name: str) -> None:
        for path in sorted(set(before) | set(after)):
            old = before.get(path) or {"exists": False, "sha256": None}
            new = after.get(path) or {"exists": False, "sha256": None}
            if _file_token(old) == _file_token(new):
                continue
            if old.get("unsupported") or new.get("unsupported"):
                continue
            self._record_task(path, kind="add" if not old.get("exists") else "delete" if not new.get("exists") else "modify",
                              before=old.get("bytes"), after=new.get("bytes"),
                              before_info=old, after_info=new, call_id=call_id, tool_name=tool_name)

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        if not self.execution_root.is_dir():
            raise ChangeJournalError("the execution workspace no longer exists")
        result = subprocess.run(git_command(self.execution_root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"), capture_output=True, check=False)
        if result.returncode == 0:
            paths = set(filter(None, result.stdout.decode("utf-8", errors="surrogateescape").split("\0")))
        else:
            paths = set()
            for directory, folders, files in os.walk(self.execution_root, followlinks=False):
                folders[:] = [name for name in folders if name not in {".git", ".wright", "node_modules", ".venv"} and not (Path(directory) / name).is_symlink()]
                paths.update((Path(directory) / name).relative_to(self.execution_root).as_posix() for name in files)
        state = self._load()
        paths.update(state.get("baseline", {}))
        paths.update(state.get("heads", {}))
        head = self._git_head()
        return {path: {**self._read_path(path), "head_commit": head} for path in sorted(paths)}

    def _read_path(self, path: str) -> dict[str, Any]:
        try:
            target = self._target(path)
        except ChangeJournalError as exc:
            return {"exists": True, "sha256": None, "bytes": None, "unsupported": str(exc)}
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return {"exists": False, "sha256": None, "bytes": None, "missing": True}
        except OSError as exc:
            return {"exists": True, "sha256": None, "bytes": None, "unsupported": str(exc)}
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                return {"exists": True, "sha256": None, "bytes": None, "unsupported": "Only regular files support session undo."}
            digest = hashlib.sha256()
            saved = bytearray()
            binary = False
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                if len(saved) < 8192:
                    binary = binary or b"\0" in chunk[:8192 - len(saved)]
                if len(saved) + len(chunk) <= MAX_BLOB_BYTES:
                    saved.extend(chunk)
            oversized = metadata.st_size > MAX_BLOB_BYTES
            return {"exists": True, "sha256": digest.hexdigest(), "bytes": None if oversized or binary else bytes(saved),
                    "mode": stat.S_IMODE(metadata.st_mode), "binary": binary, "truncated": oversized,
                    "size": metadata.st_size}

    def _target(self, value: str) -> Path:
        parts = _relative_parts(value)
        if not self.execution_root.is_dir():
            raise ChangeJournalError("the execution workspace no longer exists")
        target = self.execution_root.joinpath(*parts)
        current = self.execution_root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ChangeJournalError("symbolic links do not support safe session undo")
        if target == self.execution_root:
            raise ChangeJournalError("a file path is required")
        return target

    def _relative(self, absolute: Path) -> str | None:
        try:
            absolute = absolute.expanduser().absolute()
            # macOS spells /tmp as /private/tmp after resolve(), and a user
            # may also select a symlink to the workspace itself. Find that
            # spelling of the root without resolving away any components
            # *inside* it: those must still pass _target's symlink checks.
            # Outermost-first also rejects root/loop -> root aliases inside
            # the workspace rather than treating them as another root.
            for ancestor in reversed(absolute.parents):
                if ancestor.resolve() != self.execution_root:
                    continue
                relative = absolute.relative_to(ancestor).as_posix()
                self._target(relative)
                return relative
        except (ValueError, OSError):
            pass
        return None

    def _blob(self, sha: str | None) -> bytes | None:
        if sha is None:
            return None
        if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
            raise ChangeJournalError("invalid stored file digest")
        blob = self.directory / "blobs" / sha
        if not blob.is_file():
            raise ChangeJournalError("stored bytes for this change are missing")
        data = blob.read_bytes()
        if _sha(data) != sha:
            raise ChangeJournalError("stored bytes failed an integrity check")
        return data

    def _store_blob(self, data: bytes | None) -> tuple[str | None, bool]:
        if data is None:
            return None, True
        sha = _sha(data)
        if len(data) > MAX_BLOB_BYTES or b"\0" in data[:8192]:
            return sha, False
        directory = self.directory / "blobs"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / str(sha)
        if not destination.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=".blob-", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return sha, True

    def _patch_details(self, path: str, raw: dict[str, Any]) -> dict[str, Any]:
        details: dict[str, Any] = {"patch": "", "additions": 0, "deletions": 0,
                                   "binary": bool(raw.get("binary")), "truncated": bool(raw.get("truncated"))}
        if details["binary"] or details["truncated"]:
            details.update(unavailable=True, reason="Binary or oversized task snapshots do not support text diff or undo.")
            return details
        try:
            before = self._blob(raw.get("before_sha256")) or b""
            after = self._blob(raw.get("after_sha256")) or b""
            old_lines = before.decode("utf-8").splitlines(keepends=True)
            new_lines = after.decode("utf-8").splitlines(keepends=True)
        except UnicodeError:
            details.update(binary=True, unavailable=True, reason="This task snapshot is not UTF-8 text; text diff and undo are unavailable.")
            return details
        except ChangeJournalError as exc:
            details.update(unavailable=True, reason=str(exc))
            return details
        mode_changed = raw.get("before_mode") is not None and raw.get("after_mode") is not None and raw["before_mode"] != raw["after_mode"]
        if before == after and not mode_changed:
            return details
        diff = list(difflib.unified_diff(old_lines, new_lines,
                    fromfile=f"a/{path}" if raw.get("before_sha256") else "/dev/null",
                    tofile=f"b/{path}" if raw.get("after_sha256") else "/dev/null"))
        details["additions"] = sum(line.startswith("+") for line in diff[2:])
        details["deletions"] = sum(line.startswith("-") for line in diff[2:])
        modes = f"old mode {stat.S_IFREG | raw['before_mode']:06o}\nnew mode {stat.S_IFREG | raw['after_mode']:06o}\n" if mode_changed else ""
        patch = f"diff --git a/{path} b/{path}\n" + modes + "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)
        if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            details.update(truncated=True, unavailable=True, reason="The task patch is too large to display safely.")
        else:
            details["patch"] = patch
        return details

    def _git_head(self) -> str:
        result = subprocess.run(git_command(self.execution_root, "rev-parse", "--verify", "HEAD"), capture_output=True, check=False)
        return result.stdout.decode().strip() if result.returncode == 0 else ""

    def _committed(self, path: str, raw: dict[str, Any], info: dict[str, Any]) -> bool:
        if info.get("sha256") != raw.get("after_sha256") or bool(info.get("exists")) != raw.get("after_exists", raw.get("after_sha256") is not None):
            return False
        head = self._git_head()
        if not head or head == raw.get("recorded_head"):
            return False
        if raw.get("after_mode") is not None and raw.get("after_sha256") is not None:
            tree = subprocess.run(git_command(self.execution_root, "ls-tree", "HEAD", "--", path), capture_output=True, check=False)
            expected_mode = b"100755" if raw["after_mode"] & 0o111 else b"100644"
            if not tree.stdout.startswith(expected_mode + b" "):
                return False
        # Revision object paths are repository-relative unless prefixed ./.
        # Pathspec-based commands above already honor the execution cwd.
        result = subprocess.run(git_command(self.execution_root, "show", f"HEAD:./{path}"), capture_output=True, check=False)
        if raw.get("after_sha256") is None:
            return result.returncode != 0
        return result.returncode == 0 and _sha(result.stdout) == raw.get("after_sha256")

    def _crosses_commit(self, path: str, raw: dict[str, Any]) -> bool:
        """A partial/index commit may contain an earlier task write, not the
        latest after snapshot. It still makes the cumulative undo historical.
        Unrelated commits with no change to this path do not block undo.
        """
        base = raw.get("commit_base", raw.get("recorded_head"))
        current = self._git_head()
        if not current or base is None or base == current:
            return False
        if not base:
            result = subprocess.run(git_command(self.execution_root, "ls-tree", "HEAD", "--", path), capture_output=True, check=False)
            return result.returncode != 0 or bool(result.stdout)
        result = subprocess.run(git_command(self.execution_root, "diff", "--quiet", str(base), "HEAD", "--", path), capture_output=True, check=False)
        return result.returncode != 0

    def _revision(self, state: dict[str, Any], current: dict[str, dict[str, Any]]) -> str:
        index = subprocess.run(git_command(self.execution_root, "ls-files", "--stage", "-z"), capture_output=True, check=False)
        data = {"heads": state.get("heads", {}), "head": self._git_head(),
                "index": hashlib.sha256(index.stdout).hexdigest(),
                "files": {path: _file_token(current.get(path) or self._read_path(path)) for path in state.get("heads", {})}}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    def _load(self) -> dict[str, Any]:
        path = self.directory / "journal.json"
        if not path.is_file():
            return {"baseline": {}, "baseline_ready": False, "heads": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ChangeJournalError("the session change journal is damaged; it was not replaced") from exc
        if not isinstance(data, dict) or not isinstance(data.get("heads", {}), dict) or not isinstance(data.get("baseline", {}), dict):
            raise ChangeJournalError("invalid session change journal; it was not replaced")
        data.setdefault("baseline", {})
        data.setdefault("heads", {})
        data.setdefault("baseline_ready", False)
        return data

    def _save(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / "journal.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _relative_parts(value: str) -> tuple[str, ...]:
    relative = PurePosixPath(value)
    if not value or not relative.parts or relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts or "\\" in value or "\0" in value:
        raise ChangeJournalError("invalid session change path")
    return relative.parts


def _file_token(info: dict[str, Any]) -> tuple[Any, ...]:
    return (bool(info.get("exists", not info.get("missing", False))), info.get("sha256"), info.get("mode"), info.get("unsupported"))


def _annotate_renames(changes: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    for item in changes:
        calls = item.get("calls")
        if not isinstance(calls, list) or not calls:
            continue
        call_id = str(calls[-1].get("call_id") or "")
        if item.get("kind") == "delete" and item.get("before_sha256"):
            grouped.setdefault((call_id, str(item["before_sha256"])), {"delete": [], "add": []})["delete"].append(item)
        elif item.get("kind") == "add" and item.get("after_sha256"):
            grouped.setdefault((call_id, str(item["after_sha256"])), {"delete": [], "add": []})["add"].append(item)
    for group in grouped.values():
        if len(group["delete"]) == 1 and len(group["add"]) == 1:
            source, destination = group["delete"][0], group["add"][0]
            source.update(rename_with=destination["path"], display_kind="rename", new_path=destination["path"])
            destination.update(rename_with=source["path"], display_kind="rename", old_path=source["path"])
            # One logical rename is shown as one row. Its text is unchanged,
            # and its undo owns both stored heads rather than only the new path.
            can_revert = bool(source.get("can_revert") and destination.get("can_revert"))
            conflict = source.get("state") == "conflict" or destination.get("state") == "conflict"
            unsafe = source.get("state") == "unsafe" or destination.get("state") == "unsafe"
            history = bool(source.get("history") or destination.get("history"))
            committed = bool(source.get("committed") and destination.get("committed"))
            reason = "One path of this rename was changed outside the task; both paths must be safe to undo." if conflict else source.get("reason") or destination.get("reason") or ""
            for item in (source, destination):
                item.update(additions=0, deletions=0, can_revert=can_revert, reason=reason, history=history, committed=committed)
                if conflict:
                    item["state"] = "conflict"
                elif unsafe:
                    item["state"] = "unsafe"


def _status_paths(root: Path) -> list[tuple[str, str]]:
    result = subprocess.run(git_command(root, "status", "--porcelain=v1", "--untracked-files=all", "-z"), capture_output=True, check=False)
    if result.returncode != 0:
        return []
    parts = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    entries = []
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        status, path = item[:2], item[3:]
        if status[:1] in {"R", "C"} and index < len(parts):
            source = parts[index]
            index += 1
            entries.append(("D ", source))
        entries.append((status, path))
    return entries


def _head_from_dict(path: str, raw: dict[str, Any]) -> ChangeHead:
    return ChangeHead(path=path,
        kind=raw.get("kind") if raw.get("kind") in {"add", "modify", "delete"} else "modify",
        origin="task" if raw.get("origin") == "task" else "preexisting",
        review="accepted" if raw.get("review") == "accepted" else "pending",
        reversible=bool(raw.get("reversible")), before_sha256=raw.get("before_sha256"), after_sha256=raw.get("after_sha256"))


__all__ = ["ChangeJournalError", "SessionChangeJournal"]
