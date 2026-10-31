"""Session worker thread. Command acceptance stays on SessionService."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from ...core.logger import get_logger
from ..session_runtime.errors import SessionClosedError

logger = get_logger(__name__)

if TYPE_CHECKING:
    from ..composition.runtime import WrightRuntime


RuntimeShutdown = Callable[["WrightRuntime"], None]


class SessionRunner:
    """Own the event-consumer thread and defer resource cleanup until it exits."""

    def __init__(
        self,
        runtime: WrightRuntime,
        consume: Callable[[str, object], bool],
        *,
        shutdown: RuntimeShutdown,
    ) -> None:
        self.runtime = runtime
        self._consume = consume
        self._shutdown = shutdown
        self._lock = threading.RLock()
        self._state = "new"
        self._closed = threading.Event()
        self._cleanup_started = False
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def thread(self) -> threading.Thread | None:
        with self._lock:
            return self._thread

    def start(self) -> None:
        with self._lock:
            if self._state == "new":
                self._state = "running"
                self.runtime.session_state.lifecycle = "open"
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"wright-session-{self.runtime.session_state.session_id}",
                    daemon=True,
                )
                self._thread.start()
                return
            if self._state == "running":
                return
            raise SessionClosedError("session cannot be restarted after close was requested")

    def request_close(self) -> None:
        close_without_worker = False
        with self._lock:
            if self._state in {"closed", "closing"}:
                return
            self._state = "closing"
            self.runtime.session_state.lifecycle = "closing"
            close_without_worker = self._thread is None
        if close_without_worker:
            self._finish()
        else:
            self.runtime.event_queue.put(("EXIT", None))

    def join(self, timeout: float | None = None) -> bool:
        thread = self.thread
        if thread is None:
            return self._closed.is_set()
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        try:
            self._settle_resumed_run()
            while True:
                event_type, payload = self.runtime.event_queue.get()
                try:
                    if self._consume(event_type, payload):
                        break
                except Exception as exc:
                    # One event failing must not take the session down with
                    # it. The turn itself is already recorded as failed by
                    # the dispatcher; this only reports what escaped it.
                    logger.error("session event %s failed", event_type, exc_info=True)
                    self.runtime.agent_idle.set()
                    self.runtime.publisher.publish(
                        "system.notice", {"text": f"session event failed: {exc}"}
                    )
        except Exception as exc:
            logger.error("session worker stopped", exc_info=True)
            self.runtime.agent_idle.set()
            self.runtime.publisher.publish(
                "system.notice", {"text": f"session worker error: {exc}"}
            )
        finally:
            self._finish()

    def _settle_resumed_run(self) -> None:
        """Decide what happens to a Run that a previous process left running.

        Interactive surfaces never call the model on their own when a
        checkpoint is opened: the previous process may have died because of
        that very request. The Run is marked interrupted and the person sends
        the next message. Durable hosts opt in to automatic continuation.
        """
        if not getattr(self.runtime, "resumed", False):
            return
        session = self.runtime.session_state
        status = getattr(session, "current_run_status", None)
        if not callable(status) or status() != "running":
            return
        if getattr(self.runtime, "auto_continue_resumed", False):
            self.runtime.agent_idle.clear()
            try:
                self.runtime.agent.continue_run()
            finally:
                self.runtime.agent_idle.set()
            return
        mark = getattr(session, "mark_interrupted", None)
        if callable(mark):
            mark(error="The previous Wright process stopped before this run finished.")
            checkpoint = getattr(self.runtime.agent, "checkpoint", None)
            if callable(checkpoint):
                checkpoint()
            self.runtime.publisher.publish(
                "system.notice",
                {"text": "The previous run was interrupted. Send a message to continue."},
            )

    def _finish(self) -> None:
        """Release dependencies exactly once, after the consumer is finished."""
        with self._lock:
            self._state = "closing"
            self.runtime.session_state.lifecycle = "closing"
            if self._cleanup_started:
                return
            self._cleanup_started = True
        try:
            self._shutdown(self.runtime)
        finally:
            with self._lock:
                self._state = "closed"
                self.runtime.session_state.lifecycle = "closed"
                self._closed.set()
