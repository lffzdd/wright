"""Runtime ownership for background sub-agents.

This module deliberately does not call ``Agent.run``.  Workers only execute
isolated child sessions and enqueue terminal records; the REPL is the single
consumer that decides when to start the next root turn.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
import queue
import threading
from typing import Any, Callable

from .coordination import AgentControlPlane


class AgentBackgroundRuntime:
    """Session-scoped executor plus a thread-safe notification sink."""

    def __init__(self, event_queue: "queue.Queue[tuple[str, Any]]", *, max_workers: int = 8) -> None:
        self.event_queue = event_queue
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wright-agent")
        self._futures: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._closed = False

    def submit(
        self,
        task_id: str,
        run: Callable[[], Any],
        control: AgentControlPlane,
        *,
        done_event: str = "TASK_DONE",
        done_payload: Any | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("后台 Agent runtime 已关闭")
            future = self._executor.submit(run)
            self._futures[task_id] = future
        event_name = done_event
        payload = task_id if done_payload is None else done_payload

        def done(completed: Future) -> None:
            # The lifecycle normally owns finish_task.  Cover executor-level
            # cancellation/crashes too, so notifications are always terminal.
            try:
                if completed.cancelled():
                    control.finish_task(
                        task_id, status="cancelled", steps_used=0,
                        error="Background Agent cancelled before start",
                    )
                else:
                    error = completed.exception()
                    if error is not None:
                        control.finish_task(
                            task_id, status="failed", steps_used=0,
                            error=f"Background Agent worker failed: {type(error).__name__}: {error}",
                        )
                record = control.get(task_id)
                if record.status in {"completed", "failed", "cancelled", "timed_out"}:
                    # Durable runs use DURABLE_RUN_FINISHED so the REPL can
                    # render a summary without injecting into the root turn.
                    self.event_queue.put((event_name, payload))
            finally:
                with self._lock:
                    self._futures.pop(task_id, None)

        future.add_done_callback(done)

    def shutdown(self, control: AgentControlPlane, *, grace_seconds: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = list(self._futures.values())
        for task in control.tree(None):
            self._cancel_running(task, control)
        if futures:
            wait(futures, timeout=grace_seconds)
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _cancel_running(node: dict[str, Any], control: AgentControlPlane) -> None:
        if node.get("status") in {"pending", "running"}:
            control.request_cancel(str(node["id"]), "Root session is exiting")
        for child in node.get("children", []):
            AgentBackgroundRuntime._cancel_running(child, control)
