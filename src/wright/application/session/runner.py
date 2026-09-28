"""Session worker thread. Command acceptance stays on SessionService."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from .errors import SessionClosedError

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
            # A completed checkpoint must never produce a second Agent run.
            if (
                getattr(self.runtime, "resumed", False)
                and self.runtime.session_state.current_run_status() == "running"
            ):
                self.runtime.agent_idle.clear()
                try:
                    self.runtime.agent.continue_run()
                finally:
                    self.runtime.agent_idle.set()
            while True:
                event_type, payload = self.runtime.event_queue.get()
                if self._consume(event_type, payload):
                    break
        except Exception as exc:
            self.runtime.agent_idle.set()
            self.runtime.publisher.publish(
                "system.notice", {"text": f"session worker error: {exc}"}
            )
        finally:
            self._finish()

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
