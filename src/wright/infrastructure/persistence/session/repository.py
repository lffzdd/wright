"""File-based session repository.

This module locates checkpoint files, reads them, and replaces them atomically.
Encoding, link checks, and interrupted-tool recovery live beside it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ....core.logger import get_logger
from ....domain.gateway.session_repository import ISessionRepository
from ....domain.model.session import Session
from .codec import (
    _SESSION_ID_PATTERN,
    _checkpoint_run_status,
)
from .codec import (
    _deserialize_session as _decode_session,
)
from .codec import (
    _serialize_session as _encode_session,
)
from .errors import CheckpointError as _CheckpointError

logger = get_logger(__name__)

__all__ = ["FileSessionRepository"]


class FileSessionRepository(ISessionRepository):
    """File-based session repository implementation storing atomic JSON snapshots per session."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._save_lock = threading.RLock()

    def path_for(self, session_id: str) -> Path:
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise _CheckpointError("非法 session_id")
        return self.directory / f"{session_id}.json"

    def save(self, session: Session) -> Path:
        with self._save_lock:
            return self._save_unlocked(session)

    def _save_unlocked(self, session: Session) -> Path:
        path = self.path_for(session.session_id)
        if not session.workspace_dir.is_dir():
            raise _CheckpointError(
                f"不能保存不可恢复的会话，workspace_dir 不存在: {session.workspace_dir}"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = _encode_session(session)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{session.session_id}.",
            suffix=".tmp",
            dir=self.directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return path

    def relabel(self, session_id: str, label: str) -> dict[str, Any]:
        """Write a display name onto a checkpoint without requiring a live workspace."""

        path = self.path_for(session_id)
        with self._save_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise _CheckpointError(f"checkpoint 不存在: {session_id}") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise _CheckpointError(f"checkpoint 无法读取: {exc}") from exc
            session = data.setdefault("session", {})
            if not isinstance(session, dict):
                raise _CheckpointError(f"checkpoint 无法读取: {session_id}")
            session["session_label"] = label
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{session_id}.",
                suffix=".tmp",
                dir=self.directory,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return {"session_id": session_id, "user_goal": label, "active": False}

    def load(self, session_id: str) -> Session:
        path = self.path_for(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise _CheckpointError(f"checkpoint 不存在: {session_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise _CheckpointError(f"checkpoint 无法读取: {exc}") from exc
        return _decode_session(data)

    def latest_session_id(self) -> str | None:
        if not self.directory.is_dir():
            return None
        candidates = [
            path
            for path in self.directory.glob("*.json")
            if _SESSION_ID_PATTERN.fullmatch(path.stem)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime_ns).stem

    def load_latest(self) -> Session:
        session_id = self.latest_session_id()
        if session_id is None:
            raise _CheckpointError("没有可继续的 checkpoint")
        return self.load(session_id)

    def list_recent_sessions(self, limit: int = 5) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        candidates = [
            path
            for path in self.directory.glob("*.json")
            if _SESSION_ID_PATTERN.fullmatch(path.stem)
        ]
        if not candidates:
            return []
        sorted_paths = sorted(
            candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True
        )[:limit]

        results: list[dict[str, Any]] = []
        for path in sorted_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session_info = data.get("session", {})
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
                    "user_goal": session_info.get(
                        "session_label", session_info.get("user_goal", "")
                    ),
                    "environment": session_info.get("environment", "local"),
                    "execution_root": session_info.get("workspace_dir", ""),
                    "recoverable": bool(
                        session_info.get("workspace_dir")
                        and Path(session_info["workspace_dir"]).is_dir()
                    ),
                })
            except Exception:
                logger.debug("skip unreadable checkpoint %s", path, exc_info=True)
                continue
        return results
