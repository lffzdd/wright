"""UI-independent session commands and the session worker lifecycle.

``SessionService`` is the only owner of user-command de-duplication, queued
input cancellation, interaction resolution and model persistence.  The runner
only consumes the existing runtime event protocol; Agent and TaskService remain
the execution owners.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .runs import TERMINAL_RUN_STATUSES
from .session_models import available_models

if TYPE_CHECKING:
    from .runtime import WrightRuntime


class SessionServiceError(RuntimeError):
    """A UI-neutral error caused by a session operation."""


class SessionClosedError(SessionServiceError):
    pass


class SessionInteractionError(SessionServiceError):
    pass


EventProcessor = Callable[["WrightRuntime", str, object], bool]
RuntimeShutdown = Callable[["WrightRuntime"], None]


def set_session_model(runtime: WrightRuntime, model: str) -> tuple[str, str]:
    """Mutate and persist model configuration, rolling back on save failure."""
    if not runtime.agent_idle.is_set():
        raise SessionServiceError("cannot change model while turn is running")
    cleaned = model.strip()
    if not cleaned:
        raise SessionServiceError("model name cannot be empty")
    current = str(runtime.llm.model)
    agent_llm = getattr(runtime.agent, "llm", None)
    previous_session_model = runtime.session_state.model_name
    if cleaned == current:
        return current, cleaned
    runtime.llm.model = cleaned
    if agent_llm is not None and agent_llm is not runtime.llm:
        agent_llm.model = cleaned
    runtime.session_state.model_name = cleaned
    store = getattr(runtime.agent, "checkpoint_store", None)
    if store is not None:
        try:
            store.save(runtime.session_state)
        except Exception as exc:
            runtime.llm.model = current
            if agent_llm is not None and agent_llm is not runtime.llm:
                agent_llm.model = current
            runtime.session_state.model_name = previous_session_model
            raise SessionServiceError(f"checkpoint failed: {exc}") from exc
    publisher = getattr(runtime, "publisher", None)
    if publisher is not None:
        publisher.publish(
            "session.status_changed",
            {"session_id": runtime.session_state.session_id, "model": cleaned},
        )
    return current, cleaned


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


class SessionService:
    """Common application service used by CLI, TUI and Web adapters."""

    def __init__(
        self,
        runtime: WrightRuntime,
        *,
        event_processor: EventProcessor | None = None,
        shutdown: RuntimeShutdown | None = None,
    ) -> None:
        if event_processor is None:
            from .session_host import process_session_event

            event_processor = process_session_event
        if shutdown is None:
            from .runtime import shutdown_runtime

            shutdown = shutdown_runtime
        self.runtime = runtime
        self._event_processor = event_processor
        self.publisher = runtime.publisher
        self.interactions = runtime.interaction_broker or getattr(runtime.renderer, "_hub", None)
        self._lock = threading.RLock()
        self._commands: set[str] = set()
        self._command_order: deque[str] = deque(maxlen=2_000)
        self._queued: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()
        self._active_command: str | None = None
        self.runner = SessionRunner(runtime, self._consume, shutdown=shutdown)

    @property
    def session_id(self) -> str:
        return self.runtime.session_state.session_id

    @property
    def closed(self) -> bool:
        return self.runner.state == "closed"

    def start(self) -> None:
        self.runner.start()

    def _require_accepting_input(self) -> None:
        if self.runner.state in {"closing", "closed"}:
            raise SessionClosedError("session is closed")

    def _remember_command(self, command_id: str) -> bool:
        with self._lock:
            if command_id in self._commands:
                return False
            if len(self._command_order) == self._command_order.maxlen:
                self._commands.discard(self._command_order.popleft())
            self._commands.add(command_id)
            self._command_order.append(command_id)
            return True

    def _attachments(self, attachment_ids: list[str]) -> list[dict[str, object]]:
        if not attachment_ids:
            return []
        try:
            return [
                record.to_dict()
                for record in self.runtime.session_state.attachment_records(attachment_ids)
            ]
        except (AttributeError, ValueError) as exc:
            raise SessionServiceError(str(exc)) from exc

    def submit(
        self,
        prompt: str,
        command_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_accepting_input()
        cleaned = prompt.strip()
        attachment_ids = list(attachment_ids or [])
        attachments = self._attachments(attachment_ids)
        if not cleaned and not attachments:
            raise SessionServiceError("prompt or attachment is required")
        command_id = command_id or uuid4().hex
        fresh = self._remember_command(command_id)
        queued = not self.runtime.agent_idle.is_set() or not self.runtime.event_queue.empty()
        if fresh:
            with self._lock:
                self._queued[command_id] = {"prompt": cleaned, "attachments": attachments}
            self.runtime.event_queue.put((
                "USER_INPUT",
                {"prompt": cleaned, "command_id": command_id, "attachment_ids": attachment_ids},
            ))
        return self.publisher.publish(
            "command.accepted",
            {
                "command_id": command_id,
                "command": "turn.submit",
                "prompt": cleaned,
                "attachments": attachments,
                "queued": queued,
                "duplicate": not fresh,
            },
        ).to_dict()

    def cancel_current(
        self, command_id: str | None = None, *, cancel_queued: bool = False
    ) -> dict[str, Any]:
        self._require_accepting_input()
        command_id = command_id or uuid4().hex
        fresh = self._remember_command(command_id)
        cancelled_queued = 0
        if fresh:
            self.runtime.cancellation_event.set()
            run = getattr(self.runtime.session_state, "active_run", lambda: None)()
            if run is not None and run.status == "running":
                run.status = "cancelling"
            self._cancel_interactions(close=False)
            if cancel_queued:
                with self._lock:
                    self._cancelled.update(self._queued)
                    cancelled_queued = len(self._queued)
        return self.publisher.publish(
            "command.accepted",
            {
                "command_id": command_id,
                "command": "turn.cancel",
                "duplicate": not fresh,
                "cancelled_queued": cancelled_queued if fresh else 0,
            },
        ).to_dict()

    def cancel_queued(self, command_id: str, target_command_id: str) -> dict[str, Any]:
        self._require_accepting_input()
        if not command_id or not target_command_id:
            raise SessionServiceError("command_id and target_command_id are required")
        fresh = self._remember_command(command_id)
        with self._lock:
            queued = target_command_id in self._queued
            if fresh and queued:
                self._cancelled.add(target_command_id)
        event_type = "command.accepted" if queued or not fresh else "command.rejected"
        return self.publisher.publish(event_type, {
            "command_id": command_id,
            "command": "turn.cancel_queued",
            "target_command_id": target_command_id,
            "duplicate": not fresh,
            "reason": "" if queued or not fresh else "queued instruction is no longer pending",
        }).to_dict()

    def respond_interaction(
        self, command_id: str, request_id: str, answer: Any
    ) -> dict[str, Any]:
        self._require_accepting_input()
        if not command_id:
            raise SessionServiceError("command_id is required")
        fresh = self._remember_command(command_id)
        resolver = getattr(self.interactions, "resolve", None)
        resolved = bool(resolver(request_id, answer)) if fresh and callable(resolver) else False
        event_type = "command.accepted" if resolved or not fresh else "command.rejected"
        return self.publisher.publish(event_type, {
            "command_id": command_id,
            "command": "interaction.respond",
            "request_id": request_id,
            "duplicate": not fresh,
            "reason": "" if resolved or not fresh else "interaction is no longer pending",
        }).to_dict()

    def set_model(self, model: str) -> dict[str, Any]:
        self._require_accepting_input()
        set_session_model(self.runtime, model)
        return self.summary()

    def summary(self) -> dict[str, Any]:
        state = self.runtime.session_state
        lifecycle_state = self.runner.state
        run_status = getattr(state, "current_run_status", None)
        agent_status = run_status() if callable(run_status) else "idle"
        return {
            "session_id": state.session_id,
            "status": "closing" if lifecycle_state == "closing" else (
                "closed" if lifecycle_state == "closed" else
                ("running" if not self.runtime.agent_idle.is_set() else "idle")
            ),
            "agent_status": agent_status,
            # Legacy API field, projected from the Run owner rather than a
            # second writable session-level task field.
            "user_goal": state.current_goal() if hasattr(state, "current_goal") else state.user_goal,
            "model": state.model_name,
            "transport": getattr(state, "llm_transport", None),
            "environment": state.environment,
            "execution_root": str(state.workspace_dir),
            "project_root": str(state.project_root or state.workspace_dir),
            "base_commit": state.base_commit,
            "branch_name": state.branch_name,
            "request_context_tokens": getattr(state, "request_context_tokens", 0),
            "history_context_tokens": getattr(state, "context_tokens", 0),
            "pending_interactions": len(self._interaction_snapshot()),
            "active": lifecycle_state not in {"closing", "closed"},
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            queued = [
                {"command_id": command_id, **turn}
                for command_id, turn in self._queued.items()
                if command_id not in self._cancelled
            ]
            active_command = self._active_command
        run = getattr(self.runtime.session_state, "active_run", lambda: None)()
        active_run = None
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            active_run = {
                "run_id": run.run_id, "source": run.source, "goal": run.goal,
                "status": run.status, "step_ids": list(run.step_ids),
                "usage": dict(run.usage), "tools": [
                    {"call_id": item.call.id, "tool_name": item.call.name, "status": item.status,
                     "result": item.result.to_dict() if item.result else None}
                    for item in self.runtime.session_state.tool_executions.values()
                    if item.run_id == run.run_id
                ],
            }
        return {
            "session": self.summary(),
            "queued_commands": queued,
            "queue_depth": len(queued),
            "active_command_id": active_command,
            "active_run": active_run,
            "pending_interactions": self._interaction_snapshot(),
            "models": list(available_models(str(self.runtime.session_state.model_name or ""))),
        }

    def close(self, *, wait_timeout: float | None = None) -> bool:
        if self.runner.state == "closed":
            return True
        self.runtime.cancellation_event.set()
        self._cancel_interactions(close=True)
        # A close is not a request to run the rest of a now-detached queue.
        # Events remain in FIFO order, but are rejected by _consume before they
        # reach Agent; this lets the worker observe its EXIT event safely.
        with self._lock:
            self._cancelled.update(self._queued)
        self.runner.request_close()
        return self.runner.join(wait_timeout)

    def _interaction_snapshot(self) -> list[dict[str, Any]]:
        snapshot = getattr(self.interactions, "snapshot", None)
        return list(snapshot()) if callable(snapshot) else []

    def _cancel_interactions(self, *, close: bool) -> None:
        if self.interactions is None:
            return
        method = getattr(self.interactions, "close" if close else "cancel_pending", None)
        if callable(method):
            method()

    def _consume(self, event_type: str, payload: object) -> bool:
        command_id = (
            str(payload.get("command_id", ""))
            if event_type == "USER_INPUT" and isinstance(payload, dict)
            else ""
        )
        if command_id:
            with self._lock:
                self._queued.pop(command_id, None)
                cancelled = command_id in self._cancelled
                self._cancelled.discard(command_id)
                if not cancelled:
                    self._active_command = command_id
            if cancelled:
                self.publisher.publish(
                    "command.rejected",
                    {"command_id": command_id, "command": "turn.submit", "reason": "queued instruction cancelled before start"},
                )
                return False
        try:
            return self._event_processor(self.runtime, event_type, payload)
        finally:
            if command_id:
                with self._lock:
                    if self._active_command == command_id:
                        self._active_command = None

    @property
    def _event_processor(self) -> EventProcessor:
        # Stored on the runner closure through this property to keep tests able
        # to inject a fake processor without exposing UI implementation details.
        return self.__dict__["_processor"]

    @_event_processor.setter
    def _event_processor(self, value: EventProcessor) -> None:
        self.__dict__["_processor"] = value
