"""Polling scheduler that materializes triggers and dispatches one root run."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .models import AutomationRecord, DurableRunRecord
from .store import AutonomyStore, AutonomyStoreError
from .triggers import probe_public_web_page


class AutonomyScheduler:
    """Owns trigger polling, never Agent/session mutation.

    The scheduler writes durable state and puts ``DURABLE_RUN_DUE`` into the
    root event queue.  Only the REPL thread constructs a durable session;
    workers then run it in isolation.
    """

    def __init__(
        self,
        store: AutonomyStore,
        event_queue: queue.Queue[tuple[str, object]],
        *,
        poll_interval: float = 0.5,
        web_probe: Callable[[str], dict[str, Any]] | None = None,
        # Default 1: concurrent durable sessions share one workspace and would
        # race on the same files/shell. Raise this only with git worktree
        # isolation.
        max_inflight: int = 1,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        if max_inflight < 1:
            raise ValueError("max_inflight must be >= 1")
        self.store = store
        self.event_queue = event_queue
        self.poll_interval = float(poll_interval)
        self.web_probe = web_probe or probe_public_web_page
        self.max_inflight = int(max_inflight)
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._web_executor: ThreadPoolExecutor | None = None
        self._web_inflight: set[str] = set()
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self.store.recover_interrupted()
            self._web_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="wright-autonomy-web",
            )
            self._thread = threading.Thread(
                target=self._run,
                name="wright-autonomy-scheduler",
                daemon=True,
            )
            self._thread.start()

    def close(self, *, timeout: float = 6.0) -> None:
        executor: ThreadPoolExecutor | None = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            executor = self._web_executor
            self._web_executor = None
        self._wake.set()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if thread is not None:
            thread.join(timeout=timeout)

    def notify_changed(self) -> None:
        self._wake.set()

    def emit_event(self, name: str, payload: dict[str, Any] | None = None) -> int:
        event_id = self.store.emit_event(name, payload)
        self.notify_changed()
        return event_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> DurableRunRecord:
        try:
            return self.store.finish_run(
                run_id,
                status=status,
                result=result,
                error=error,
            )
        finally:
            self.notify_changed()

    def runtime_event(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        trigger_payload: dict[str, Any] = run.trigger_payload
        encoded_payload = json.dumps(
            trigger_payload, ensure_ascii=False, default=repr
        )
        if len(encoded_payload) > 2_000:
            trigger_payload = {
                "truncated": True,
                "preview": encoded_payload[:2_000],
            }
        return {
            "type": "durable_task_due",
            "task": {
                "id": run.id,
                "kind": "durable",
                "schedule_id": run.automation_id,
                "name": run.automation_name[:200],
                "prompt": run.prompt[:4_000],
                "trigger_type": run.trigger_type,
                "trigger_payload": trigger_payload,
                "attempt": run.attempt,
                "max_retries": run.max_retries,
            },
        }

    def _run(self) -> None:
        while True:
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
            try:
                if self.store.closed:
                    return
                self.store.materialize_due()
                self._poll_web_changes()
                if self.store.count_active_runs() < self.max_inflight:
                    run = self.store.claim_next_run()
                    if run is not None:
                        with self._lock:
                            if self._closed:
                                return
                        self.event_queue.put(("DURABLE_RUN_DUE", run.id))
            except Exception as exc:
                if self._closed or self.store.closed:
                    return
                # Scheduler failures must be observable but cannot kill the
                # input/Agent loop. The root thread decides how to render them.
                self.event_queue.put((
                    "AUTONOMY_ERROR",
                    f"{type(exc).__name__}: {exc}",
                ))
            self._wake.wait(timeout=self.poll_interval)

    def _poll_web_changes(self) -> None:
        executor = self._web_executor
        if executor is None or self._closed:
            return
        for automation in self.store.list_due_web_probes():
            with self._lock:
                if self._closed:
                    return
                if automation.id in self._web_inflight:
                    continue
                self._web_inflight.add(automation.id)
            try:
                executor.submit(self._probe_web, automation)
            except RuntimeError:
                with self._lock:
                    self._web_inflight.discard(automation.id)
                return

    def _probe_web(self, automation: AutomationRecord) -> None:
        try:
            if self._closed or self.store.closed:
                return
            snapshot = self.web_probe(automation.trigger.url)
            if self._closed or self.store.closed:
                return
            self.store.record_web_probe(automation.id, snapshot)
            self.notify_changed()
        except Exception as exc:
            if self._closed or self.store.closed:
                return
            try:
                self.store.defer_web_probe(
                    automation.id, f"{type(exc).__name__}: {exc}"
                )
            except AutonomyStoreError:
                return
            self.event_queue.put((
                "AUTONOMY_ERROR",
                (f"web trigger {automation.id} probe failed: "
                f"{type(exc).__name__}: {exc}"),
            ))
        finally:
            with self._lock:
                self._web_inflight.discard(automation.id)
