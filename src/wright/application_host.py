"""Application-scoped owner for durable automation execution.

Unlike ``SessionService``, this host has no conversation queue, renderer, or
source Session.  A Session may create an Automation, but an already persisted
Automation is subsequently executed by the host using its own control plane
and background supervisor.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .agent_background import AgentBackgroundRuntime
from .autonomy import AutonomyScheduler, AutonomyStore
from .autonomy.runner import launch_durable_run
from .coordination import AgentControlPlane
from .llm import LLMClient
from .permission import PermissionSettings
from .services import RuntimeServices
from .tools.base import Tool


@dataclass(frozen=True)
class ApplicationHostSnapshot:
    project_dir: str
    source_session_id: str
    state: str
    scheduler_host_id: str
    active_runs: int


class ApplicationHost:
    """Own one explicitly opened project's durable services and workers."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        store: AutonomyStore,
        llm: LLMClient,
        base_tools: Sequence[Tool],
        permission_settings: PermissionSettings,
        on_event: Callable[[str, object], None] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.workspace_dir = workspace_dir.resolve()
        self.store = store
        self.llm = llm
        self.base_tools = tuple(base_tools)
        self.permission_settings = permission_settings
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.control_plane = AgentControlPlane()
        self.background = AgentBackgroundRuntime(self.event_queue)
        self._on_event = on_event
        self._lock = threading.RLock()
        self._state = "new"
        self._closed = threading.Event()
        self._lock_fd: int | None = None
        self.scheduler = AutonomyScheduler(
            store,
            self.event_queue,
            poll_interval=poll_interval,
            dispatch_run=self._dispatch,
        )
        self.services = RuntimeServices(
            agent_background=self.background,
            durable_store=store,
            autonomy_scheduler=self.scheduler,
        )

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def start(self) -> None:
        with self._lock:
            if self._state == "running":
                return
            if self._state != "new":
                raise RuntimeError("application host cannot be restarted after close")
            self._state = "running"
        try:
            self._acquire_project_lock()
            self.scheduler.start()
        except Exception:
            with self._lock:
                self._state = "failed"
            self.close()
            raise

    def _acquire_project_lock(self) -> None:
        """Acquire one advisory owner lock for this SQLite project scope.

        SQLite's atomic claim handles normal concurrent polling, while this
        lock prevents a second host from classifying a live owner's running
        rows as crash recovery. ``flock`` is released by the OS if its process
        dies, so it cannot leave a stale PID file behind.
        """
        # Store rows are source-session scoped.  Different source sessions can
        # legitimately have independent schedules in one project database;
        # two owners of the *same* scope cannot.
        scope = hashlib.sha256(self.store.session_id.encode("utf-8")).hexdigest()[:16]
        lock_path = self.store.path.with_suffix(self.store.path.suffix + f".{scope}.host.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"another ApplicationHost already owns {self.workspace_dir}"
            ) from exc
        self._lock_fd = fd

    def _release_project_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _dispatch(self, run_id: str) -> None:
        """Launch a claimed run without borrowing a source Session resource."""
        with self._lock:
            if self._state != "running":
                return
        try:
            launch_durable_run(
                run_id=run_id,
                workspace_dir=self.workspace_dir,
                control_plane=self.control_plane,
                scheduler=self.scheduler,
                llm=self.llm,
                base_tools=self.base_tools,
                permission_settings=self.permission_settings,
                background_runtime=self.background,
                services=self.services,
            )
        except Exception as exc:
            try:
                self.scheduler.finish_run(
                    run_id, status="failed", error=f"host launch failed: {exc}"
                )
            except Exception:
                pass
            self._publish("AUTONOMY_ERROR", f"host launch failed: {exc}")

    def _publish(self, event: str, payload: object) -> None:
        if self._on_event is not None:
            self._on_event(event, payload)

    def snapshot(self) -> ApplicationHostSnapshot:
        return ApplicationHostSnapshot(
            project_dir=str(self.workspace_dir),
            source_session_id=self.store.session_id,
            state=self.state,
            scheduler_host_id=self.scheduler.host_id,
            active_runs=self.store.count_active_runs(),
        )

    def close(self, *, grace_seconds: float = 2.0) -> bool:
        with self._lock:
            if self._state in {"closed", "closing"}:
                return self._state == "closed"
            self._state = "closing"
        self.scheduler.close(timeout=grace_seconds)
        if self.background.shutdown(self.control_plane, grace_seconds=grace_seconds):
            self._finish_close()
            return True
        # Do not tear down the durable store beneath an unavoidable synchronous
        # model/tool call.  A watcher performs final cleanup only after its
        # worker leaves the executor.
        watcher = threading.Thread(
            target=self._wait_then_finish_close,
            name="wright-application-host-close",
            daemon=True,
        )
        watcher.start()
        return False

    def _wait_then_finish_close(self) -> None:
        self.background.wait_for_idle()
        self._finish_close()

    def _finish_close(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
        self.store.close()
        self._release_project_lock()
        with self._lock:
            self._state = "closed"
            self._closed.set()
