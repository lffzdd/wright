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
from copy import copy
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .agent_background import AgentBackgroundRuntime
from .artifacts import ArtifactStore
from .autonomy import AutonomyScheduler, AutonomyStore
from .autonomy.models import DurableRunRecord
from .autonomy.runner import launch_durable_run
from .coordination import AgentControlPlane
from .llm import LLMClient
from .permission import PermissionSettings
from .services import RuntimeServices
from .tools.base import Tool
from .tools.mcp_client import McpManager, McpServerConfig


@dataclass(frozen=True)
class ApplicationHostSnapshot:
    project_dir: str
    source_session_ids: tuple[str, ...]
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
        mcp_configs: Sequence[McpServerConfig] = (),
        artifact_store: ArtifactStore | None = None,
        on_event: Callable[[str, object], None] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.workspace_dir = workspace_dir.resolve()
        self.store = store
        self.llm = copy(llm)
        self.base_tools = tuple(base_tools)
        self._host_tools = self.base_tools
        # Durable work must never borrow the Session's MCP manager: closing a
        # browser session tears that manager down while this host may live on.
        self.mcp_manager = McpManager(list(mcp_configs), artifact_store=artifact_store)
        self.permission_settings = permission_settings
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.control_plane = AgentControlPlane()
        self.background = AgentBackgroundRuntime(self.event_queue)
        self._on_event = on_event
        self._lock = threading.RLock()
        self._state = "new"
        self._closed = threading.Event()
        self._lock_fd: int | None = None
        self._event_thread: threading.Thread | None = None
        self._poll_interval = poll_interval
        self._host_id = f"host_{uuid4().hex}"
        self._schedulers: dict[str, AutonomyScheduler] = {}
        self.scheduler = self.scheduler_for(store)
        self.services = RuntimeServices(
            agent_background=self.background,
            durable_store=store,
            autonomy_scheduler=self.scheduler,
        )

    def scheduler_for(self, store: AutonomyStore) -> AutonomyScheduler:
        """Retain source-session isolation under one execution-directory owner.

        The host takes ownership of the supplied store, including closing a
        redundant connection when a previously opened session is resumed.
        """
        with self._lock:
            if self._state not in {"new", "running"}:
                raise RuntimeError("application host is closing")
            if store.workspace_dir != self.workspace_dir or store.path != self.store.path:
                raise ValueError("automation store belongs to another execution environment")
            existing = self._schedulers.get(store.session_id)
            if existing is not None:
                if store is not existing.store:
                    store.close()
                return existing
            scheduler = AutonomyScheduler(
                store, self.event_queue, poll_interval=self._poll_interval,
                host_id=self._host_id,
                dispatch_run=lambda run_id: self._dispatch(run_id, scheduler),
                claim_run=lambda: self._claim_next_run(scheduler),
            )
            self._schedulers[store.session_id] = scheduler
            if self._state == "running":
                try:
                    scheduler.start()
                except Exception:
                    self._schedulers.pop(store.session_id)
                    scheduler.close()
                    store.close()
                    raise
            return scheduler

    def _claim_next_run(self, scheduler: AutonomyScheduler) -> DurableRunRecord | None:
        # Claim and the shared capacity check must be indivisible across all
        # source sessions: per-session limits alone allow concurrent writers.
        with self._lock:
            if self._state != "running" or self._active_runs() >= 1:
                return None
            return scheduler.store.claim_next_run(owner_id=self._host_id)

    def _active_runs(self) -> int:
        with self._lock:
            if self._state == "closed":
                return 0
            return sum(scheduler.store.count_active_runs() for scheduler in self._schedulers.values())

    def has_active_work(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return False
            if self._state == "closing":
                return True
            return bool(self._active_runs()) or any(
                item.status == "active"
                for scheduler in self._schedulers.values()
                for item in scheduler.store.list_automations()
            )

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def start(self) -> None:
        try:
            # Keep new source registrations behind MCP/lock initialization.
            # Scheduler threads may start here, but cannot claim until this
            # lock is released with the host fully assembled.
            with self._lock:
                if self._state == "running":
                    return
                if self._state != "new":
                    raise RuntimeError("application host cannot be restarted after close")
                self._acquire_project_lock()
                self._host_tools = (*self.base_tools, *self.mcp_manager.start())
                self._event_thread = threading.Thread(
                    target=self._forward_events, name="wright-application-events", daemon=True
                )
                self._event_thread.start()
                self._state = "running"
                for scheduler in self._schedulers.values():
                    scheduler.start()
        except Exception:
            with self._lock:
                if self._state in {"closing", "closed"}:
                    raise
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
        # Durable definitions retain their source-session association, but
        # process ownership is an execution-environment concern: two hosts
        # pointed at one worktree/local checkout could otherwise run unknown
        # writes concurrently merely because their source sessions differ.
        scope = hashlib.sha256(str(self.workspace_dir).encode("utf-8")).hexdigest()[:16]
        lock_path = self.store.path.parent / f"{scope}.host.lock"
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

    def _dispatch(self, run_id: str, scheduler: AutonomyScheduler) -> None:
        """Launch a claimed run without borrowing a source Session resource."""
        with self._lock:
            if self._state != "running":
                return
        try:
            launch_durable_run(
                run_id=run_id,
                workspace_dir=self.workspace_dir,
                control_plane=self.control_plane,
                scheduler=scheduler,
                llm=self.llm,
                base_tools=self._host_tools,
                permission_settings=self.permission_settings,
                background_runtime=self.background,
                services=replace(
                    self.services, durable_store=scheduler.store, autonomy_scheduler=scheduler,
                ),
            )
        except Exception as exc:
            try:
                scheduler.finish_run(
                    run_id, status="failed", error=f"host launch failed: {exc}"
                )
            except Exception:
                pass
            self._publish("AUTONOMY_ERROR", f"host launch failed: {exc}")

    def _publish(self, event: str, payload: object) -> None:
        if self._on_event is not None:
            self._on_event(event, payload)

    def _forward_events(self) -> None:
        """Drain durable completion/error notifications; never retain them unbounded."""
        while True:
            event, payload = self.event_queue.get()
            if event == "__HOST_EXIT__":
                return
            self._publish(event, payload)

    def snapshot(self) -> ApplicationHostSnapshot:
        with self._lock:
            return ApplicationHostSnapshot(
                project_dir=str(self.workspace_dir),
                source_session_ids=tuple(self._schedulers),
                state=self._state,
                scheduler_host_id=self._host_id,
                active_runs=self._active_runs(),
            )

    def close(self, *, grace_seconds: float = 2.0) -> bool:
        with self._lock:
            if self._state in {"closed", "closing"}:
                return self._state == "closed"
            self._state = "closing"
            schedulers = tuple(self._schedulers.values())
        for scheduler in schedulers:
            scheduler.close(timeout=grace_seconds)
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
        self.event_queue.put(("__HOST_EXIT__", None))
        thread = self._event_thread
        if thread is not None:
            thread.join(timeout=1)
        self.mcp_manager.shutdown()
        with self._lock:
            for scheduler in self._schedulers.values():
                scheduler.store.close()
            self._release_project_lock()
            self._state = "closed"
            self._closed.set()
