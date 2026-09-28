"""Per-session record of file bytes a task wrote.

The journal lives under the project state directory, not in the edited tree.
Revert copies stored bytes back. It does not reset or clean the git checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from ...core.paths import session_dir
from ...domain.model.workspace.review import ChangeHead, disposition, preflight

MAX_BLOB_BYTES = 1_000_000


class ChangeJournalError(ValueError):
    pass


class SessionChangeJournal:
    def __init__(self, session: Any) -> None:
        project = Path(getattr(session, "project_root", None) or session.workspace_dir)
        self.project_root = project.expanduser().resolve()
        self.execution_root = Path(session.workspace_dir).expanduser().resolve()
        self.session_id = str(session.session_id)
        self.directory = session_dir(self.project_root) / "changes" / self.session_id
        self._lock = threading.RLock()
        self._held: dict[str, dict[str, Any]] = {}

    def note_text(
        self,
        absolute: Path,
        *,
        before: str | None,
        after: str,
        existed: bool,
        call_id: str,
        tool_name: str,
    ) -> None:
        relative = self._relative(absolute)
        if relative is None:
            return
        before_bytes = None if before is None else before.encode("utf-8")
        after_bytes = after.encode("utf-8")
        kind = "modify" if existed else "add"
        self._record_task(
            relative,
            kind=kind,
            before=before_bytes,
            after=after_bytes,
            call_id=call_id,
            tool_name=tool_name,
        )

    def capture(self) -> dict[str, dict[str, Any]]:
        self.ensure_baseline()
        return self._snapshot()

    def commit_capture(
        self,
        before: dict[str, dict[str, Any]],
        *,
        call_id: str,
        tool_name: str,
    ) -> None:
        after = self._snapshot()
        self._apply_delta(before, after, call_id=call_id, tool_name=tool_name)

    def hold(self, command_id: str, before: dict[str, dict[str, Any]], *, call_id: str) -> None:
        with self._lock:
            self._held[command_id] = {"before": before, "call_id": call_id}

    def finish_held(self, command_id: str) -> None:
        with self._lock:
            held = self._held.pop(command_id, None)
        if held is None:
            return
        self.commit_capture(held["before"], call_id=held["call_id"], tool_name="execute_command")

    def ensure_baseline(self) -> None:
        with self._lock:
            state = self._load()
            if state.get("baseline_ready"):
                return
            state["baseline"] = {
                path: item["sha256"]
                for path, item in self._snapshot().items()
            }
            state["baseline_ready"] = True
            self._save(state)

    def projection(self) -> dict[str, Any]:
        self.ensure_baseline()
        with self._lock:
            state = self._load()
        current = self._snapshot()
        baseline = state.get("baseline") or {}
        heads = state.get("heads") or {}
        paths = sorted(set(baseline) | set(heads) | set(current))
        changes: list[dict[str, Any]] = []
        for path in paths:
            info = current.get(path)
            missing = bool(info and info.get("missing"))
            current_sha = None if info is None or missing else info.get("sha256")
            exists = info is not None and not missing
            if path in heads:
                head = _head_from_dict(path, heads[path])
                live = disposition(head, current_sha256=current_sha, exists=exists)
                changes.append({
                    **heads[path],
                    "path": path,
                    "state": live,
                    "current_sha256": current_sha,
                })
                continue
            expected = baseline.get(path)
            if expected is None and current_sha is None:
                continue
            if expected is not None and current_sha == expected:
                changes.append({
                    "path": path,
                    "origin": "preexisting",
                    "kind": "modify",
                    "state": "preexisting",
                    "review": "pending",
                    "reversible": False,
                    "before_sha256": expected,
                    "after_sha256": expected,
                    "current_sha256": current_sha,
                })
                continue
            if expected != current_sha:
                changes.append({
                    "path": path,
                    "origin": "external",
                    "kind": "add" if expected is None else "modify",
                    "state": "external",
                    "review": "pending",
                    "reversible": False,
                    "before_sha256": expected,
                    "after_sha256": current_sha,
                    "current_sha256": current_sha,
                })
        _annotate_renames(changes)
        return {
            "session_id": self.session_id,
            "execution_root": str(self.execution_root),
            "changes": changes,
            "semantics": {
                "accept": "mark a task edit as reviewed; the bytes are already on disk",
                "revert": "restore bytes from before this task first changed the file",
            },
        }

    def accept(self, paths: list[str], *, confirm: bool) -> dict[str, Any]:
        return self._batch("accept", paths, confirm=confirm)

    def revert(self, paths: list[str], *, confirm: bool) -> dict[str, Any]:
        return self._batch("revert", paths, confirm=confirm)

    def _batch(self, action: str, paths: list[str], *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise ChangeJournalError("confirmation is required")
        if not paths:
            raise ChangeJournalError("at least one path is required")
        unique: list[str] = []
        for path in paths:
            if path not in unique:
                unique.append(path)
        view = {item["path"]: item for item in self.projection()["changes"]}
        states = []
        for path in unique:
            item = view.get(path)
            if item is None:
                states.append((path, "external"))
            else:
                states.append((path, str(item["state"])))
        checked = preflight(action, states)  # type: ignore[arg-type]
        if any(item["result"] == "blocked" for item in checked):
            return {"ok": False, "applied": False, "action": action, "results": checked}
        applied: list[str] = []
        try:
            for path in unique:
                if action == "accept":
                    self._mark_accepted(path)
                else:
                    self._restore_before(path)
                applied.append(path)
        except Exception as exc:
            if action == "revert":
                for path in reversed(applied):
                    self._restore_after(path)
            return {
                "ok": False,
                "applied": False,
                "action": action,
                "error": str(exc),
                "results": [
                    {**item, "result": "rolled_back" if item["path"] in applied else item["result"]}
                    for item in checked
                ],
            }
        if action == "revert":
            with self._lock:
                state = self._load()
                for path in unique:
                    state.get("heads", {}).pop(path, None)
                self._save(state)
        return {
            "ok": True,
            "applied": True,
            "action": action,
            "results": [{**item, "result": "applied"} for item in checked],
        }

    def _mark_accepted(self, path: str) -> None:
        with self._lock:
            state = self._load()
            head = state["heads"][path]
            head["review"] = "accepted"
            self._save(state)

    def _restore_before(self, path: str) -> None:
        with self._lock:
            state = self._load()
            head = state["heads"][path]
        target = self.execution_root / path
        self._write_restored(target, head.get("before_sha256"), kind=head["kind"])

    def _restore_after(self, path: str) -> None:
        with self._lock:
            state = self._load()
            head = (state.get("heads") or {}).get(path)
        if head is None:
            return
        target = self.execution_root / path
        try:
            self._write_restored(target, head.get("after_sha256"), kind="modify")
        except ChangeJournalError:
            return

    def _write_restored(self, target: Path, sha: str | None, *, kind: str) -> None:
        if sha is None:
            if target.exists():
                if target.is_dir():
                    raise ChangeJournalError(f"{target.name} is a directory and cannot be reverted")
                target.unlink()
            elif kind == "delete":
                raise ChangeJournalError(f"stored bytes for {target.name} are missing")
            return
        blob = self.directory / "blobs" / sha
        if not blob.is_file():
            raise ChangeJournalError("stored bytes for this change are missing")
        data = blob.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ChangeJournalError("stored bytes failed an integrity check")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.wright-revert")
        temporary.write_bytes(data)
        os.replace(temporary, target)

    def _record_task(
        self,
        relative: str,
        *,
        kind: str,
        before: bytes | None,
        after: bytes | None,
        call_id: str,
        tool_name: str,
    ) -> None:
        before_sha, before_ok = self._store_blob(before)
        after_sha, after_ok = self._store_blob(after)
        with self._lock:
            state = self._load()
            if not state.get("baseline_ready"):
                state["baseline"] = {
                    path: item["sha256"]
                    for path, item in self._snapshot().items()
                    if path != relative
                }
                if before_sha is not None:
                    state["baseline"][relative] = before_sha
                state["baseline_ready"] = True
            heads = state.setdefault("heads", {})
            previous = heads.get(relative)
            reversible = bool(before_ok and after_ok) or (kind == "add" and before is None and after_ok)
            if kind == "delete":
                reversible = before_ok
            record = {
                "path": relative,
                "origin": "task",
                "kind": kind if previous is None else previous.get("kind", kind),
                "review": "pending",
                "reversible": reversible if previous is None else bool(previous.get("reversible") and reversible),
                "before_sha256": previous.get("before_sha256") if previous else before_sha,
                "after_sha256": after_sha,
                "calls": [
                    *(previous.get("calls", []) if previous else []),
                    {
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "after_sha256": after_sha,
                    },
                ],
            }
            if previous is None and kind == "add":
                record["before_sha256"] = None
            heads[relative] = record
            self._save(state)

    def _apply_delta(
        self,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
        *,
        call_id: str,
        tool_name: str,
    ) -> None:
        paths = set(before) | set(after)
        for path in sorted(paths):
            old = before.get(path)
            new = after.get(path)
            old_sha = None if old is None else old.get("sha256")
            new_sha = None if new is None else new.get("sha256")
            new_missing = bool(new and new.get("missing"))
            old_missing = bool(old and old.get("missing"))
            if new_missing and old_missing:
                continue
            if new_missing or (new is None and old is not None and not old_missing):
                before_bytes = None if old is None or old_missing else _blob_bytes(old)
                if before_bytes is None:
                    before_bytes = self._head_bytes(path)
                self._record_task(
                    path, kind="delete", before=before_bytes, after=None,
                    call_id=call_id, tool_name=tool_name,
                )
                continue
            if old_sha == new_sha:
                continue
            if new is None:
                self._record_task(
                    path, kind="delete", before=_blob_bytes(old), after=None,
                    call_id=call_id, tool_name=tool_name,
                )
                continue
            kind = "add" if old is None else "modify"
            before_bytes = _blob_bytes(old)
            if before_bytes is None and old is None:
                before_bytes = self._head_bytes(path)
                if before_bytes is not None:
                    kind = "modify"
            self._record_task(
                path, kind=kind, before=before_bytes, after=_blob_bytes(new),
                call_id=call_id, tool_name=tool_name,
            )

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        root = self.execution_root
        if not root.is_dir():
            return {}
        snapshot: dict[str, dict[str, Any]] = {}
        for status, path in _status_paths(root):
            absolute = root / path
            if not absolute.is_file():
                if "D" in status:
                    snapshot[path] = {"sha256": None, "bytes": None, "missing": True}
                continue
            data = _read_limited(absolute)
            sha = hashlib.sha256(absolute.read_bytes()).hexdigest() if absolute.stat().st_size <= MAX_BLOB_BYTES else None
            if sha is None:
                digest = hashlib.sha256()
                with absolute.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                sha = digest.hexdigest()
            snapshot[path] = {"sha256": sha, "bytes": data}
        return snapshot

    def _head_bytes(self, relative: str) -> bytes | None:
        result = subprocess.run(
            ["git", "-C", str(self.execution_root), "show", f"HEAD:{relative}"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0 or len(result.stdout) > MAX_BLOB_BYTES or b"\0" in result.stdout[:8192]:
            return None
        return result.stdout

    def _store_blob(self, data: bytes | None) -> tuple[str | None, bool]:
        if data is None:
            return None, True
        sha = hashlib.sha256(data).hexdigest()
        reversible = len(data) <= MAX_BLOB_BYTES and b"\0" not in data[:8192]
        if not reversible:
            return sha, False
        destination = self.directory / "blobs" / sha
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        return sha, True

    def _relative(self, absolute: Path) -> str | None:
        try:
            resolved = absolute.expanduser().resolve()
        except OSError:
            return None
        root = self.execution_root
        if resolved != root and root not in resolved.parents:
            return None
        relative = resolved.relative_to(root).as_posix()
        if relative in {"", "."} or ".." in relative.split("/"):
            return None
        return relative

    def _load(self) -> dict[str, Any]:
        path = self.directory / "journal.json"
        if not path.is_file():
            return {"baseline": {}, "baseline_ready": False, "heads": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"baseline": {}, "baseline_ready": False, "heads": {}}
        if not isinstance(data, dict):
            return {"baseline": {}, "baseline_ready": False, "heads": {}}
        data.setdefault("baseline", {})
        data.setdefault("heads", {})
        data.setdefault("baseline_ready", False)
        return data

    def _save(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / "journal.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)


def _blob_bytes(item: dict[str, Any] | None) -> bytes | None:
    if item is None:
        return None
    data = item.get("bytes")
    return data if isinstance(data, bytes) else None


def _read_limited(path: Path) -> bytes | None:
    size = path.stat().st_size
    if size > MAX_BLOB_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data


def _annotate_renames(changes: list[dict[str, Any]]) -> None:
    """Pair a delete and an add from the same call when the bytes match.

    The stored kinds stay delete and add so revert can restore the source and
    remove the destination. The pair is what the workbench shows as a rename.
    """

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
        if len(group["delete"]) != 1 or len(group["add"]) != 1:
            continue
        source = group["delete"][0]
        destination = group["add"][0]
        source["rename_with"] = destination["path"]
        destination["rename_with"] = source["path"]
        source["display_kind"] = "rename"
        destination["display_kind"] = "rename"


def _status_paths(root: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    parts = result.stdout.decode("utf-8", errors="replace").split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        status = item[:2]
        path = item[3:]
        if status[:1] in {"R", "C"} and index < len(parts):
            destination = parts[index]
            index += 1
            if path:
                entries.append(("D ", path))
            if destination:
                entries.append((status, destination))
            continue
        if path:
            entries.append((status, path))
    return entries


def _head_from_dict(path: str, raw: dict[str, Any]) -> ChangeHead:
    kind = raw.get("kind") if raw.get("kind") in {"add", "modify", "delete"} else "modify"
    origin = "task" if raw.get("origin") == "task" else "preexisting"
    review = "accepted" if raw.get("review") == "accepted" else "pending"
    return ChangeHead(
        path=path,
        kind=kind,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
        review=review,  # type: ignore[arg-type]
        reversible=bool(raw.get("reversible")),
        before_sha256=raw.get("before_sha256"),
        after_sha256=raw.get("after_sha256"),
    )


__all__ = ["ChangeJournalError", "SessionChangeJournal"]
