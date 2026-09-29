"""Process-local catalog of open sessions and their execution hosts.

Web and TUI both open sessions through this service. It keeps the active
runtime capacity, the shared directory coordinator, and ApplicationHost
instances. HTTP status codes and terminal widgets stay in the adapters.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...core.paths import project_id, session_dir, task_db_path
from ...infrastructure.llm.model_adapters import available_models, process_model_name
from ...infrastructure.persistence.autonomy_store import (
    AutonomyNotFoundError,
    AutonomyStore,
)
from ...infrastructure.persistence.session.errors import CheckpointError
from ...infrastructure.persistence.session.repository import FileSessionRepository
from ...infrastructure.workspace.project import ProjectContext
from ...infrastructure.workspace.worktrees import ArchiveResult, WorktreeManager
from ..composition.host import ApplicationHost
from ..composition.runtime import (
    RuntimeConfig,
    WrightRuntime,
    assemble_runtime,
    shutdown_runtime,
)
from ..execution.directory import DirectoryExecutionCoordinator
from .opening import OpenIntentError, load_resume_checkpoint, resolve_new_environment
from .publisher import EventPublisher
from .service import SessionService

logger = logging.getLogger(__name__)

AssembleRuntime = Callable[..., WrightRuntime]


class SessionDirectoryError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class OpenSession:
    """One live conversation. The runtime stays behind the session service."""

    runtime: WrightRuntime
    service: SessionService
    execution_root: Path

    @property
    def session_id(self) -> str:
        return self.runtime.session_state.session_id

    @property
    def publisher(self) -> EventPublisher:
        return self.runtime.publisher

    @property
    def interactions(self) -> Any:
        return self.runtime.interaction_broker

    def close(self, *, wait_timeout: float | None = 5) -> bool:
        return self.service.close(wait_timeout=wait_timeout)


class SessionDirectory:
    """Open, resume, close, and archive sessions for one project process."""

    def __init__(
        self,
        project_root: Path,
        *,
        capacity: int = 4,
        base_args: argparse.Namespace | None = None,
        assemble: AssembleRuntime = assemble_runtime,
        coordinator: DirectoryExecutionCoordinator | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("session capacity must be positive")
        self.project_root = project_root.expanduser().resolve()
        if not self.project_root.is_dir():
            raise SessionDirectoryError(f"workspace does not exist: {self.project_root}")
        self.capacity = capacity
        self.base_args = base_args or argparse.Namespace()
        self.coordinator = coordinator or DirectoryExecutionCoordinator()
        self.worktrees = WorktreeManager(self.project_root)
        self.checkpoints = FileSessionRepository(session_dir(self.project_root))
        self._assemble = assemble
        self._sessions: dict[str, OpenSession] = {}
        self._hosts: dict[Path, ApplicationHost] = {}
        self._root_locks: dict[Path, threading.Lock] = {}
        self._request_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()

    def project(self) -> dict[str, Any]:
        base_model = process_model_name(getattr(self.base_args, "model", None))
        with self._lock:
            active_count = len(self._sessions)
        return {
            "project_id": project_id(self.project_root),
            "name": self.project_root.name,
            "project_root": str(self.project_root),
            "git": self.worktrees.is_git,
            "capacity": self.capacity,
            "active_count": active_count,
            "default_environment": "local",
            "dirty_checkout": bool(
                self.worktrees.is_git
                and self.worktrees.inspect(ProjectContext.local(self.project_root))["dirty"]
            ),
            "default_model": base_model,
            "models": list(available_models(base_model)),
            **_git_fields(self.project_root),
        }

    def open(
        self,
        *,
        environment: str | None = None,
        model: str | None = None,
        resume: str | None = None,
        continue_latest: bool = False,
        interaction_broker: Any = None,
        publisher: EventPublisher | None = None,
        workspace: Path | None = None,
        resume_chooser: Callable[[list[dict]], str] | None = None,
        session_id: str | None = None,
        client_request_id: str | None = None,
    ) -> OpenSession:
        """Open a new local session, an explicit worktree, or a saved checkpoint.

        An empty ``resume`` keeps the caller's choice intent. ``continue_latest``
        loads the newest checkpoint. Neither path changes the saved environment.
        A repeated ``client_request_id`` returns the session already created for it.
        """

        request_lock = (
            self._request_lock(client_request_id) if client_request_id else nullcontext()
        )
        with request_lock:
            return self._open(
                environment=environment,
                model=model,
                resume=resume,
                continue_latest=continue_latest,
                interaction_broker=interaction_broker,
                publisher=publisher,
                workspace=workspace,
                resume_chooser=resume_chooser,
                session_id=session_id,
                client_request_id=client_request_id,
            )

    def _open(
        self,
        *,
        environment: str | None,
        model: str | None,
        resume: str | None,
        continue_latest: bool,
        interaction_broker: Any,
        publisher: EventPublisher | None,
        workspace: Path | None,
        resume_chooser: Callable[[list[dict]], str] | None,
        session_id: str | None,
        client_request_id: str | None,
    ) -> OpenSession:
        if client_request_id:
            reused = self._reuse_request(client_request_id)
            if reused is not None and reused in self._sessions:
                return self.get(reused)
            if reused is not None and resume is None and not continue_latest:
                resume = reused
        created_worktree = False
        context: ProjectContext | None = None
        with self._lock:
            if len(self._sessions) >= self.capacity:
                raise SessionDirectoryError(
                    f"active session capacity {self.capacity} reached; "
                    "close a session before opening another",
                    kind="capacity",
                )
            resume_id = (resume or "").strip()
            if resume_id and resume_id in self._sessions:
                raise SessionDirectoryError(
                    "session is already active", kind="conflict",
                )
            try:
                saved = load_resume_checkpoint(
                    self.checkpoints,
                    resume=resume,
                    continue_latest=continue_latest,
                    resume_chooser=resume_chooser,
                )
            except OpenIntentError as exc:
                raise SessionDirectoryError(str(exc), kind=exc.kind) from exc
            if saved is not None:
                if saved.session_id in self._sessions:
                    raise SessionDirectoryError(
                        "session is already active", kind="conflict",
                    )
                saved_project = (saved.project_root or self.project_root).resolve()
                if saved_project != self.project_root:
                    raise SessionDirectoryError("checkpoint belongs to a different project")
                context = ProjectContext(
                    project_root=saved_project,
                    execution_root=Path(saved.workspace_dir),
                    environment=saved.environment,
                    base_commit=saved.base_commit,
                    branch_name=saved.branch_name,
                )
                if not context.execution_root.is_dir():
                    raise SessionDirectoryError(
                        "execution directory is no longer available; the checkpoint can still be viewed",
                        kind="history_only",
                    )
                session_id = saved.session_id
            else:
                session_id = session_id or uuid4().hex[:12]
                try:
                    selected = resolve_new_environment(
                        environment, git=self.worktrees.is_git,
                    )
                except OpenIntentError as exc:
                    raise SessionDirectoryError(str(exc), kind=exc.kind) from exc
                if workspace is not None:
                    requested = workspace.expanduser().resolve()
                    if requested != self.project_root:
                        raise SessionDirectoryError(
                            "workspace must be the project root; execution directory is chosen separately",
                        )
                if selected == "worktree":
                    context = self.worktrees.create(session_id)
                    created_worktree = True
                else:
                    context = ProjectContext.local(self.project_root)
            assert context is not None
            root = context.execution_root.resolve()
            config = self._runtime_config(
                context=context,
                model=model,
                resume=saved.session_id if saved is not None else None,
            )
        root_lock = self._root_lock(root)
        with root_lock:
            with self._lock:
                retained_host = self._hosts.get(root)
            try:
                runtime = self._assemble(
                    config,
                    project_context=context,
                    publisher=publisher,
                    interaction_broker=interaction_broker,
                    session_id=session_id,
                    application_host=retained_host,
                    directory_coordinator=self.coordinator,
                    resume_chooser=resume_chooser,
                )
            except BaseException:
                self._rollback_worktree(created_worktree, context)
                raise
            host = runtime.application_host
            if host is None:
                shutdown_runtime(runtime)
                self._rollback_worktree(created_worktree, context)
                raise SessionDirectoryError("runtime did not construct ApplicationHost")
            runtime.owns_application_host = False
            if getattr(runtime.session_state, "permission_mode", None) is None:
                settings = getattr(runtime, "permission_settings", None)
                if settings is not None and getattr(settings, "mode", None):
                    runtime.session_state.permission_mode = settings.mode
            try:
                from ..workspace.journal import SessionChangeJournal

                SessionChangeJournal(runtime.session_state).ensure_baseline()
            except Exception:
                logger.exception("change baseline was not captured")
            opened = OpenSession(
                runtime=runtime,
                service=SessionService(runtime, shutdown=shutdown_runtime),
                execution_root=root,
            )
            try:
                opened.service.start()
            except BaseException:
                shutdown_runtime(runtime)
                self._rollback_worktree(created_worktree, context)
                if retained_host is None and host.state != "closed":
                    host.close()
                raise
            with self._lock:
                conflict = session_id in self._sessions
                over_capacity = len(self._sessions) >= self.capacity
                if not conflict and not over_capacity:
                    self._sessions[session_id] = opened
                    self._hosts[root] = host
            if conflict or over_capacity:
                opened.close(wait_timeout=2)
                self._rollback_worktree(created_worktree, context)
                if retained_host is None and host.state != "closed":
                    host.close()
                if conflict:
                    raise SessionDirectoryError(
                        "session is already active", kind="conflict",
                    )
                raise SessionDirectoryError(
                    f"active session capacity {self.capacity} reached; "
                    "close a session before opening another",
                    kind="capacity",
                )
        if client_request_id:
            self._remember_request(client_request_id, opened.session_id)
        return opened

    def _root_lock(self, root: Path) -> threading.Lock:
        with self._lock:
            lock = self._root_locks.get(root)
            if lock is None:
                lock = threading.Lock()
                self._root_locks[root] = lock
            return lock

    def preview(self, session_id: str) -> dict[str, Any]:
        """Read a checkpoint without starting a runtime or a worktree."""

        try:
            saved = self.checkpoints.load(session_id)
        except CheckpointError as exc:
            raise SessionDirectoryError(str(exc), kind="not_found") from exc
        saved_project = (saved.project_root or self.project_root).resolve()
        if saved_project != self.project_root:
            raise SessionDirectoryError("checkpoint belongs to a different project")
        from .history_projection import project_history, seed_ids

        turn_ids, run_ids = seed_ids(saved)
        execution_root = Path(saved.workspace_dir)
        from ..workspace.timeline import (
            project_accessed_files,
            project_subagents,
            project_timeline,
        )

        plan = {}
        manager = getattr(saved, "plan_manager", None)
        snapshot = getattr(manager, "snapshot", None)
        if callable(snapshot):
            plan = snapshot() or {}
        return {
            "view_only": True,
            "history": project_history(saved, turn_ids, run_ids),
            "timeline": project_timeline(saved, []),
            "subagents": project_subagents(saved),
            "accessed_files": project_accessed_files(saved),
            "plan": plan,
            "session": {
                "session_id": saved.session_id,
                "lifecycle": "closed",
                "execution": "idle",
                "status": "closed",
                "active": False,
                "user_goal": saved.current_goal() if hasattr(saved, "current_goal") else saved.user_goal,
                "model": saved.model_name,
                "interaction_mode": getattr(saved, "interaction_mode", "agent"),
                "permission_mode": getattr(saved, "permission_mode", None),
                "environment": saved.environment,
                "execution_root": str(execution_root),
                "project_root": str(saved_project),
                "base_commit": saved.base_commit,
                "branch_name": saved.branch_name,
                "recoverable": execution_root.is_dir(),
            },
        }

    def search_references(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Search the project checkout. Does not create a session or a worktree."""

        from ..workspace.references import search_file_references

        return search_file_references(self.project_root, query, limit=limit)

    def get(self, session_id: str) -> OpenSession:
        with self._lock:
            opened = self._sessions.get(session_id)
        if opened is None:
            raise SessionDirectoryError("session is not active", kind="not_found")
        return opened

    def close(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            opened = self._sessions.get(session_id)
        if opened is None:
            return {
                "session_id": session_id,
                "lifecycle": "closed",
                "status": "closed",
                "execution": "idle",
                "active": False,
            }
        self.coordinator.cancel_waiters(session_id=session_id)
        finished = opened.close(wait_timeout=5)
        summary = opened.service.summary()
        if finished:
            with self._lock:
                current = self._sessions.get(session_id)
                if current is opened:
                    self._sessions.pop(session_id, None)
        return summary

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            active = {
                key: {**value.service.summary(), "recoverable": True}
                for key, value in self._sessions.items()
            }
        results = list(active.values())
        for saved in self.checkpoints.list_recent_sessions(limit=100):
            if saved["session_id"] in active:
                continue
            results.append({**saved, "active": False, "status": "closed", "lifecycle": "closed"})
        return results

    def run_history(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            hosts = tuple(self._hosts.values())
        for host in hosts:
            try:
                return host.run_history(run_id)
            except KeyError:
                continue
        store = AutonomyStore(
            task_db_path(self.project_root),
            session_id="__history_query__",
            workspace_dir=self.project_root,
        )
        try:
            return store.run_history(run_id)
        except AutonomyNotFoundError as exc:
            raise SessionDirectoryError("durable run not found", kind="not_found") from exc
        finally:
            store.close()

    def relabel(self, session_id: str, label: str) -> dict[str, Any]:
        """Rename a live or closed session without starting execution."""

        cleaned = label.strip()
        if not cleaned:
            raise SessionDirectoryError("label is required")
        with self._lock:
            opened = self._sessions.get(session_id)
        if opened is not None:
            opened.runtime.session_state.session_label = cleaned
            opened.service.persist()
            return opened.service.summary()
        try:
            return self.checkpoints.relabel(session_id, cleaned)
        except CheckpointError as exc:
            raise SessionDirectoryError(str(exc), kind="not_found") from exc

    def archive(self, session_id: str) -> ArchiveResult:
        with self._lock:
            opened = self._sessions.get(session_id)
        if opened is not None:
            context = opened.runtime.project_context
            self.close(session_id)
            with self._lock:
                if session_id in self._sessions:
                    raise SessionDirectoryError(
                        "session is still closing and its execution directory is in use",
                        kind="busy",
                    )
        else:
            try:
                saved = self.checkpoints.load(session_id)
            except CheckpointError as exc:
                raise SessionDirectoryError(str(exc), kind="not_found") from exc
            context = ProjectContext(
                project_root=saved.project_root or self.project_root,
                execution_root=saved.workspace_dir,
                environment=saved.environment,
                base_commit=saved.base_commit,
                branch_name=saved.branch_name,
            )
        root = context.execution_root.resolve()
        with self._lock:
            users = [
                item.session_id for item in self._sessions.values()
                if item.execution_root == root
            ]
            host = self._hosts.get(root)
        if users:
            raise SessionDirectoryError(
                "worktree is still used by an open session", kind="busy",
            )
        if self.coordinator.occupied(root):
            raise SessionDirectoryError(
                "worktree still has an executing task", kind="busy",
            )
        if host is not None:
            if host.has_active_work():
                raise SessionDirectoryError(
                    "worktree is still referenced by an active automation host",
                    kind="busy",
                )
            if not host.close():
                raise SessionDirectoryError(
                    "worktree automation host is still closing", kind="busy",
                )
            with self._lock:
                if self._hosts.get(root) is host:
                    self._hosts.pop(root, None)
        return self.worktrees.archive(context)

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            hosts = list(self._hosts.values())
            self._hosts.clear()
        for opened in sessions:
            try:
                opened.close(wait_timeout=5)
            except Exception:
                logger.exception("session close during directory shutdown failed")
        for host in hosts:
            host.close()

    def _request_lock(self, request_id: str) -> threading.Lock:
        with self._lock:
            lock = self._request_locks.get(request_id)
            if lock is None:
                lock = threading.Lock()
                self._request_locks[request_id] = lock
            return lock

    def _request_path(self) -> Path:
        return session_dir(self.project_root) / "open_requests.json"

    def _reuse_request(self, request_id: str) -> str | None:
        path = self._request_path()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        session_id = data.get(request_id)
        if not isinstance(session_id, str) or not session_id:
            return None
        with self._lock:
            if session_id in self._sessions:
                return session_id
        try:
            self.checkpoints.load(session_id)
        except CheckpointError:
            return None
        return session_id

    def _remember_request(self, request_id: str, session_id: str) -> None:
        path = self._request_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        current: dict[str, str] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                current = {
                    str(key): str(value)
                    for key, value in loaded.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
        current[request_id] = session_id
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        os.replace(temporary, path)

    def _runtime_config(
        self, *, context: ProjectContext, model: str | None, resume: str | None,
    ) -> RuntimeConfig:
        base = self.base_args
        hooks = getattr(base, "hooks_config", None)
        workspace = context.execution_root
        return RuntimeConfig(
            workspace=workspace,
            resume=resume,
            continue_latest=False,
            no_session_persistence=bool(getattr(base, "no_session_persistence", False)),
            hooks_config=Path(hooks) if hooks else None,
            model=model,
            transport=getattr(base, "transport", None),
            trust_project_mcp=bool(getattr(base, "trust_project_mcp", False)),
            with_rag=bool(getattr(base, "with_rag", False)),
            mode=str(getattr(base, "mode", "coding") or "coding"),
        )

    def _rollback_worktree(self, created: bool, context: ProjectContext | None) -> None:
        if not created or context is None:
            return
        try:
            self.worktrees.archive(context)
        except Exception:
            logger.exception("failed to roll back worktree %s", context.execution_root)


def _git_fields(root: Path) -> dict[str, Any]:
    try:
        from ..workspace.git_status import summarize_git

        summary = summarize_git(root)
    except Exception:
        logger.exception("git summary failed")
        return {"branch": None, "uncommitted_count": 0}
    return {
        "branch": summary.get("branch"),
        "uncommitted_count": summary.get("uncommitted_count", 0),
    }


__all__ = ["OpenSession", "SessionDirectory", "SessionDirectoryError"]
