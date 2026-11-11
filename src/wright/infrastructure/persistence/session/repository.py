"""File-based session repository.

This module locates checkpoint files, reads them, and replaces them atomically.
Encoding, link checks, and interrupted-tool recovery live beside it.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ....core.logger import get_logger
from ....domain.gateway.session_repository import ISessionRepository
from ....domain.model.session import INTERACTIVE_SESSION_LABEL, Session
from ....domain.model.session.errors import CheckpointError as _CheckpointError
from ...permission_transaction import (
    atomic_write,
    register_checkpoint_root,
    transaction,
)
from .codec import (
    _SESSION_ID_PATTERN,
    CHECKPOINT_VERSION,
    _checkpoint_run_status,
)
from .codec import (
    _deserialize_session as _decode_session,
)
from .codec import (
    _serialize_session as _encode_session,
)

logger = get_logger(__name__)



def _checkpoint_title(session_info: dict[str, Any]) -> str:
    """Mirror ``Session.display_title`` on raw checkpoint data.

    Their label, else the first request; a closed session must not fall back
    to the placeholder label every interactive session starts with.
    """
    label = str(session_info.get("session_label") or "")
    if label and label != INTERACTIVE_SESSION_LABEL:
        return label
    runs = session_info.get("runs")
    if isinstance(runs, dict):
        goals = [
            str(run.get("goal") or "")
            for run in runs.values()
            if isinstance(run, dict) and run.get("source") == "user_input" and run.get("goal")
        ]
        if goals:
            return goals[0]
    return label


# Files the session directory also holds that are not checkpoints.
_NON_CHECKPOINT_STEMS = frozenset({"open_requests"})

__all__ = ["FileSessionRepository"]


class FileSessionRepository(ISessionRepository):
    """File-based session repository implementation storing atomic JSON snapshots per session."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._save_lock = threading.RLock()
        register_checkpoint_root(self.directory)
        with transaction():
            pass

    def path_for(self, session_id: str) -> Path:
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise _CheckpointError("invalid session_id")
        return self.directory / f"{session_id}.json"

    def save(self, session: Session) -> Path:
        with transaction(), self._save_lock:
            return self._save_unlocked(session)

    def _save_unlocked(self, session: Session) -> Path:
        path = self.path_for(session.session_id)
        if not session.workspace_dir.is_dir():
            raise _CheckpointError(
                f"cannot save an unrecoverable session; workspace_dir does not exist: {session.workspace_dir}"
            )
        payload = _encode_session(session)

        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode())
        return path

    def relabel(self, session_id: str, label: str) -> dict[str, Any]:
        """Write a display name onto a checkpoint without requiring a live workspace."""

        path = self.path_for(session_id)
        with transaction(), self._save_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise _CheckpointError(f"checkpoint not found: {session_id}") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise _CheckpointError(f"cannot read checkpoint: {exc}") from exc
            if data.get("version") != CHECKPOINT_VERSION:
                raise _CheckpointError("Unsupported checkpoint version; only version 8 is accepted")
            session = data.setdefault("session", {})
            if not isinstance(session, dict):
                raise _CheckpointError(f"cannot read checkpoint: {session_id}")
            session["session_label"] = label
            atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode())
        return {"session_id": session_id, "user_goal": label, "active": False}

    def load(self, session_id: str) -> Session:
        return self.load_with_saved_at(session_id)[0]

    def load_with_saved_at(self, session_id: str) -> tuple[Session, float]:
        """Return one decoded checkpoint and its observation time atomically."""
        with transaction():
            path = self.path_for(session_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise _CheckpointError(f"checkpoint not found: {session_id}") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise _CheckpointError(f"cannot read checkpoint: {exc}") from exc
        session = _decode_session(data)
        try:
            saved_at = datetime.fromisoformat(data["saved_at"]).timestamp()
        except (KeyError, TypeError, ValueError) as exc:
            raise _CheckpointError("checkpoint saved_at must be an ISO timestamp") from exc
        return session, saved_at

    def _checkpoint_paths(self) -> list[Path]:
        """Checkpoint files only; other bookkeeping shares this directory."""
        if not self.directory.is_dir():
            return []
        return [
            path
            for path in self.directory.glob("*.json")
            if _SESSION_ID_PATTERN.fullmatch(path.stem)
            and path.stem not in _NON_CHECKPOINT_STEMS
        ]

    def latest_session_id(self) -> str | None:
        if not self.directory.is_dir():
            return None
        candidates = self._checkpoint_paths()
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime_ns).stem

    def load_latest(self) -> Session:
        with transaction():
            session_id = self.latest_session_id()
            if session_id is None:
                raise _CheckpointError("no checkpoint to resume")
            return self.load(session_id)

    def list_recent_sessions(self, limit: int = 5) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        candidates = self._checkpoint_paths()
        if not candidates:
            return []
        sorted_paths = sorted(
            candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True
        )[:limit]

        results: list[dict[str, Any]] = []
        for path in sorted_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session_info = data.get("session") if isinstance(data, dict) else None
                if not isinstance(session_info, dict):
                    continue  # not a session checkpoint
                saved_at = data.get("saved_at", "")
                saved_at_str = saved_at
                if saved_at:
                    try:
                        dt = datetime.fromisoformat(saved_at).astimezone()
                        saved_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        logger.debug(
                            "checkpoint timestamp parse failed: %s",
                            saved_at,
                            exc_info=True,
                        )
                results.append({
                    "session_id": path.stem,
                    "saved_at": saved_at_str,
                    "status": _checkpoint_run_status(session_info),
                    "user_goal": _checkpoint_title(session_info),
                    "environment": session_info.get("environment", "local"),
                    "execution_root": session_info.get("workspace_dir", ""),
                    "recoverable": bool(
                        data.get("version") == CHECKPOINT_VERSION
                        and session_info.get("workspace_dir")
                        and Path(session_info["workspace_dir"]).is_dir()
                    ),
                })
            except Exception:
                logger.debug("skip unreadable checkpoint %s", path, exc_info=True)
                continue
        return results
