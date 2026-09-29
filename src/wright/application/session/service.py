"""UI-independent session commands.

``SessionService`` owns user-command de-duplication, queued input
cancellation, interaction resolution and model persistence. The worker thread
lives in ``runner`` and only consumes the existing runtime event protocol.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...core.logger import get_logger
from ...domain.model.session.run import TERMINAL_RUN_STATUSES
from ...infrastructure.llm.model_adapters import available_models
from ...infrastructure.storage.attachments import AttachmentError
from .errors import SessionClosedError, SessionServiceError
from .runner import RuntimeShutdown, SessionRunner

logger = get_logger(__name__)


def _plan_brief(session_state: Any) -> str:
    manager = getattr(session_state, "plan_manager", None)
    if manager is None or not getattr(manager, "has_plan", False):
        return ""
    steps = getattr(manager, "steps", ())
    if not steps:
        return ""
    current = next((step for step in steps if step.status == "in_progress"), None)
    done = sum(1 for step in steps if step.status in {"completed", "skipped"})
    if current is not None:
        title = current.title
        if len(title) > 32:
            title = title[:29] + "…"
        return f"plan {done}/{len(steps)} {title}"
    return f"plan {getattr(manager, 'status', '')} {done}/{len(steps)}"

if TYPE_CHECKING:
    from ..composition.runtime import WrightRuntime


EventProcessor = Callable[["WrightRuntime", str, object], bool]


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
            from .dispatch import process_session_event

            event_processor = process_session_event
        if shutdown is None:
            from ..composition.runtime import shutdown_runtime

            shutdown = shutdown_runtime
        self.runtime = runtime
        self._event_processor = event_processor
        self.publisher = runtime.publisher
        self.interactions = runtime.interaction_broker
        self._lock = threading.RLock()
        self._commands: set[str] = set()
        self._command_order: deque[str] = deque(maxlen=2_000)
        self._queued: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()
        self._active_command: str | None = None
        self._active_attachment_ids: set[str] = set()
        self._staged_references: list[dict[str, Any]] = []
        # A new process receives a unique consumer id.  SQLite is the arbiter;
        # this value only identifies the winner of a claim, never grants a
        # second right to execute work.
        self._consumer_id = f"session-worker:{uuid4().hex}"
        agent = getattr(runtime, "agent", None)
        existing_run_started = getattr(agent, "on_run_started", None)

        def persist_run_id(run_id: str) -> None:
            if callable(existing_run_started):
                existing_run_started(run_id)
            with self._lock:
                command_id = self._active_command
            store = getattr(self.runtime, "autonomy_store", None)
            if command_id and store is not None:
                try:
                    # The command remains ``claimed`` until Agent has created
                    # its RunRecord.  This callback is the commit point for
                    # actual execution; a crash before it is safely
                    # recoverable instead of being misclassified as a running
                    # side effect.
                    if not store.start_command(
                        self._command_scope, command_id, self._consumer_id, run_id=run_id
                    ):
                        raise SessionServiceError(
                            "could not transition accepted command to running"
                        )
                except Exception as exc:
                    # Executing a new user turn without an accepted Run link
                    # would reintroduce the crash ambiguity this service owns.
                    raise SessionServiceError(
                        "could not persist command Run association"
                    ) from exc

        if agent is not None:
            agent.on_run_started = persist_run_id
        self.runner = SessionRunner(runtime, self._consume, shutdown=shutdown)

    @property
    def session_id(self) -> str:
        return self.runtime.session_state.session_id

    @property
    def closed(self) -> bool:
        return self.runner.state == "closed"

    def start(self) -> None:
        self._recover_durable_commands()
        self.runner.start()

    @property
    def _command_scope(self) -> str:
        return f"session:{self.session_id}"

    def _recover_durable_commands(self) -> None:
        """Requeue only work whose execution has not started.

        ``running`` is deliberately classified by the store as ``unknown``:
        an Agent may already have invoked an external tool when its process
        disappeared, so replaying would be less reliable than reporting it.
        """
        store = getattr(self.runtime, "autonomy_store", None)
        if store is None:
            return
        try:
            # Queue/Event rendezvous are process-local.  A persisted pending
            # request from a previous process must never look approvable in a
            # new UI unless an explicit resumable interaction protocol exists.
            store.terminate_pending_interactions(
                self._command_scope,
                reason="session process restarted; interaction was not resumed",
            )
            records = store.recover_commands(self._command_scope)
        except Exception as exc:
            raise SessionServiceError(f"could not recover accepted commands: {exc}") from exc
        for record in records:
            payload = record.get("payload", {})
            if payload.get("command") != "turn.submit":
                # Mutating control commands are completed when handled, not
                # replayed as an orphaned queue item.
                continue
            command_id = str(record["command_id"])
            attachment_ids = list(payload.get("attachment_ids", []))
            try:
                attachments = self._attachments(attachment_ids)
            except SessionServiceError:
                # The original attachments may have been removed.  Preserve
                # the accepted record, but do not manufacture a new prompt.
                continue
            with self._lock:
                self._queued[command_id] = {
                    "prompt": str(payload.get("prompt", "")),
                    "attachments": attachments,
                    "references": list(payload.get("references") or []),
                }
            self.runtime.event_queue.put((
                "USER_INPUT",
                {
                    "prompt": str(payload.get("prompt", "")),
                    "command_id": command_id,
                    "attachment_ids": attachment_ids,
                    "references": list(payload.get("references") or []),
                },
            ))

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

    def _accept_command(self, command_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Persist a command id before it is allowed to affect a session."""
        store = getattr(self.runtime, "autonomy_store", None)
        if store is None:
            return {}, self._remember_command(command_id)
        try:
            record, fresh = store.accept_command(
                self._command_scope, command_id, payload
            )
        except Exception as exc:
            raise SessionServiceError(f"command was not durably accepted: {exc}") from exc
        if fresh:
            self._remember_command(command_id)
        return record, fresh

    def _queue_input(
        self,
        command_id: str,
        prompt: str,
        attachment_ids: list[str],
        attachments: list[dict[str, object]],
        references: list[dict[str, Any]] | None = None,
    ) -> None:
        references = list(references or [])
        store = getattr(self.runtime, "autonomy_store", None)
        if store is not None:
            try:
                before = store.get_command(self._command_scope, command_id)
            except Exception as exc:
                raise SessionServiceError(f"could not queue accepted command: {exc}") from exc
            if str(before.get("status")) not in {"accepted"}:
                return
            try:
                record = store.queue_command(self._command_scope, command_id)
            except Exception as exc:
                raise SessionServiceError(f"could not queue accepted command: {exc}") from exc
            if record["status"] not in {"queued", "accepted"}:
                return
        with self._lock:
            already = command_id in self._queued
            if not already:
                self._queued[command_id] = {
                    "prompt": prompt,
                    "attachments": attachments,
                    "references": references,
                }
        if already:
            return
        self.runtime.event_queue.put((
            "USER_INPUT",
            {
                "prompt": prompt,
                "command_id": command_id,
                "attachment_ids": attachment_ids,
                "references": references,
            },
        ))

    @staticmethod
    def _answer_digest(answer: Any) -> str:
        encoded = json.dumps(answer, ensure_ascii=False, default=repr)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

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

    def _checkpoint(self) -> None:
        store = getattr(self.runtime.agent, "checkpoint_store", None)
        if store is None:
            return
        try:
            store.save(self.runtime.session_state)
        except Exception as exc:
            raise SessionServiceError(f"checkpoint failed: {exc}") from exc

    def _inflight_attachment_ids(self) -> set[str]:
        with self._lock:
            found = set(self._active_attachment_ids)
            for item in self._queued.values():
                for attachment in item.get("attachments") or []:
                    if isinstance(attachment, dict) and attachment.get("id"):
                        found.add(str(attachment["id"]))
            return found

    def add_attachment(self, session_id: str, filename: str, data: bytes) -> dict[str, Any]:
        """Validate and commit an attachment. The web route only transfers bytes."""

        self._require_accepting_input()
        if session_id != self.runtime.session_state.session_id:
            raise SessionServiceError("attachment session does not match")
        store = getattr(self.runtime, "attachment_store", None)
        if store is None:
            raise SessionServiceError("attachment storage is unavailable")
        try:
            record = store.register_bytes(filename, data, self.runtime.session_state.attachments)
        except AttachmentError as exc:
            raise SessionServiceError(str(exc)) from exc
        already_present = record.id in self.runtime.session_state.attachments
        self.runtime.session_state.attachments[record.id] = record
        try:
            self._checkpoint()
        except SessionServiceError:
            if not already_present:
                self.runtime.session_state.attachments.pop(record.id, None)
                try:
                    store.remove(record)
                except AttachmentError:
                    logger.warning("could not remove attachment after checkpoint failure", exc_info=True)
            raise
        return record.to_dict()

    def remove_attachment(self, session_id: str, attachment_id: str) -> None:
        self._require_accepting_input()
        if session_id != self.runtime.session_state.session_id:
            raise SessionServiceError("attachment session does not match")
        record = self.runtime.session_state.attachments.get(attachment_id)
        if record is None:
            raise SessionServiceError("attachment not found")
        if any(
            attachment_id in (message.message.get("attachments") or [])
            for message in getattr(self.runtime.session_state, "message_records", []) or []
        ):
            raise SessionServiceError("attachment is already part of conversation history")
        if attachment_id in self._inflight_attachment_ids():
            raise SessionServiceError("attachment is referenced by a queued or running command")
        store = getattr(self.runtime, "attachment_store", None)
        self.runtime.session_state.attachments.pop(attachment_id, None)
        try:
            self._checkpoint()
        except SessionServiceError:
            self.runtime.session_state.attachments[attachment_id] = record
            raise
        if store is not None:
            try:
                store.remove(record)
            except AttachmentError as exc:
                raise SessionServiceError(str(exc)) from exc

    def stage_reference(self, raw: str) -> dict[str, Any]:
        """Remember a file identity on the draft. This does not read the file."""

        self._require_accepting_input()
        from pathlib import Path

        from ..workspace.references import ReferenceError, identify_reference

        settings = getattr(self.runtime, "permission_settings", None)
        external = list(getattr(settings, "additional_directories", []) or [])
        project = Path(
            self.runtime.session_state.project_root or self.runtime.session_state.workspace_dir
        )
        try:
            ref = identify_reference(project, raw, external_roots=external)
        except ReferenceError as exc:
            raise SessionServiceError(str(exc)) from exc
        with self._lock:
            self._staged_references.append(ref)
        return ref

    def staged_references(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._staged_references)

    def submit(
        self,
        prompt: str,
        command_id: str | None = None,
        attachment_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._require_accepting_input()
        cleaned = prompt.strip()
        attachment_ids = list(attachment_ids or [])
        attachments = self._attachments(attachment_ids)
        from ..workspace.documents import DocumentError, render_documents
        from ..workspace.references import ReferenceError, prepare_submission_references

        try:
            document_text = render_documents(self.runtime.session_state, list(document_ids or []))
        except DocumentError as exc:
            raise SessionServiceError(str(exc)) from exc
        if document_text:
            cleaned = f"{cleaned}\n\n{document_text}".strip()
        project_root = self.runtime.session_state.project_root or self.runtime.session_state.workspace_dir
        settings = getattr(self.runtime, "permission_settings", None)
        external = list(getattr(settings, "additional_directories", []) or [])
        with self._lock:
            staged = list(self._staged_references)
            if references is None:
                supplied = staged
            else:
                supplied = list(references)
        try:
            prepared = prepare_submission_references(
                project_root, supplied, cleaned, external_roots=external,
            )
        except ReferenceError as exc:
            raise SessionServiceError(str(exc)) from exc
        if not cleaned and not attachments and not prepared:
            raise SessionServiceError("prompt or attachment is required")
        command_id = command_id or uuid4().hex
        record, fresh = self._accept_command(command_id, {
            "command": "turn.submit", "prompt": cleaned,
            "attachment_ids": attachment_ids,
            "references": prepared,
        })
        queued = not self.runtime.agent_idle.is_set() or not self.runtime.event_queue.empty()
        if fresh or record.get("status") == "accepted":
            self._queue_input(command_id, cleaned, attachment_ids, attachments, prepared)
            with self._lock:
                if references is None:
                    self._staged_references.clear()
        return self.publisher.publish(
            "command.accepted",
            {
                "command_id": command_id,
                "command": "turn.submit",
                "prompt": cleaned,
                "attachments": attachments,
                "references": prepared,
                "queued": queued,
                "duplicate": not fresh,
            },
        ).to_dict()

    def stop_current(self, command_id: str | None = None) -> dict[str, Any]:
        """Stop the turn that holds execution. Queued messages stay queued."""

        return self._stop(command_id, cancel_queued=False, command="turn.cancel")

    def stop_all(self, command_id: str | None = None) -> dict[str, Any]:
        """Stop the current turn and cancel every message that has not started."""

        return self._stop(command_id, cancel_queued=True, command="turn.cancel_all")

    def _stop(
        self, command_id: str | None, *, cancel_queued: bool, command: str,
    ) -> dict[str, Any]:
        self._require_accepting_input()
        command_id = command_id or uuid4().hex
        _record, fresh = self._accept_command(command_id, {
            "command": command, "cancel_queued": cancel_queued,
        })
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
                    targets = tuple(self._queued)
                store = getattr(self.runtime, "autonomy_store", None)
                if store is not None:
                    for target in targets:
                        store.cancel_command(
                            self._command_scope, target, reason="cancelled with current execution"
                        )
            store = getattr(self.runtime, "autonomy_store", None)
            if store is not None:
                store.complete_command(
                    self._command_scope, command_id,
                    {"cancel_queued": cancel_queued}, status="completed",
                )
        return self.publisher.publish(
            "command.accepted",
            {
                "command_id": command_id,
                "command": command,
                "duplicate": not fresh,
                "cancelled_queued": cancelled_queued if fresh else 0,
            },
        ).to_dict()

    def cancel_queued(self, command_id: str, target_command_id: str) -> dict[str, Any]:
        self._require_accepting_input()
        if not command_id or not target_command_id:
            raise SessionServiceError("command_id and target_command_id are required")
        _record, fresh = self._accept_command(command_id, {
            "command": "turn.cancel_queued", "target_command_id": target_command_id,
        })
        with self._lock:
            queued = target_command_id in self._queued
            if fresh and queued:
                self._cancelled.add(target_command_id)
        if fresh:
            store = getattr(self.runtime, "autonomy_store", None)
            if store is not None:
                if queued:
                    store.cancel_command(
                        self._command_scope, target_command_id, reason="cancelled before execution"
                    )
                store.complete_command(
                    self._command_scope, command_id,
                    {"target_command_id": target_command_id, "cancelled": queued},
                    status="completed" if queued else "failed",
                )
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
        _record, fresh = self._accept_command(command_id, {
            "command": "interaction.respond", "request_id": request_id,
            "answer_sha256": self._answer_digest(answer),
        })
        resolver = getattr(self.interactions, "resolve", None)
        resolved = bool(resolver(request_id, answer)) if fresh and callable(resolver) else False
        if fresh:
            store = getattr(self.runtime, "autonomy_store", None)
            if store is not None:
                store.complete_command(
                    self._command_scope, command_id,
                    {"request_id": request_id, "resolved": resolved},
                    status="completed" if resolved else "failed",
                )
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

    def set_execution_policy(
        self,
        *,
        interaction_mode: str | None = None,
        permission_mode: str | None = None,
    ) -> dict[str, Any]:
        """Apply mode changes only while this session is idle.

        An in-flight tool keeps the grant it already received. The new ceiling
        is used the next time permission is resolved.
        """

        self._require_accepting_input()
        if not self.runtime.agent_idle.is_set() or self._interaction_snapshot():
            raise SessionServiceError(
                "a turn is still executing; change the mode when the session is idle"
            )
        state = self.runtime.session_state
        if interaction_mode is not None:
            if interaction_mode not in {"agent", "plan", "ask"}:
                raise SessionServiceError("interaction_mode must be agent, plan, or ask")
            state.interaction_mode = interaction_mode
        if permission_mode is not None:
            if permission_mode not in {"default", "acceptEdits", "bypass", "plan"}:
                raise SessionServiceError(
                    "permission_mode must be default, acceptEdits, bypass, or plan"
                )
            state.permission_mode = permission_mode
        self._checkpoint()
        return {
            "session_id": state.session_id,
            "interaction_mode": state.interaction_mode,
            "permission_mode": state.permission_mode,
            "effective": "next permission resolution",
        }

    def add_document(self, session_id: str, filename: str, data: bytes) -> dict[str, Any]:
        self._require_accepting_input()
        if session_id != self.runtime.session_state.session_id:
            raise SessionServiceError("document session does not match")
        from ..workspace.documents import DocumentError, store_document

        try:
            record = store_document(self.runtime.session_state, filename, data)
        except DocumentError as exc:
            raise SessionServiceError(str(exc)) from exc
        self.runtime.session_state.documents[str(record["id"])] = record
        try:
            self._checkpoint()
        except SessionServiceError:
            self.runtime.session_state.documents.pop(str(record["id"]), None)
            raise
        return record

    def command_status(self, command_id: str) -> dict[str, Any]:
        """Return the durable acceptance record for a client retry/query."""
        if not command_id:
            raise SessionServiceError("command_id is required")
        store = getattr(self.runtime, "autonomy_store", None)
        if store is None:
            raise SessionServiceError("durable command history is unavailable")
        try:
            return store.get_command(self._command_scope, command_id)
        except Exception as exc:
            raise SessionServiceError(str(exc)) from exc

    def summary(self) -> dict[str, Any]:
        state = self.runtime.session_state
        lifecycle_state = self.runner.state
        lifecycle = "open" if lifecycle_state in {"new", "running"} else lifecycle_state
        run_status = getattr(state, "current_run_status", None)
        agent_status = run_status() if callable(run_status) else "idle"
        execution, queue_reason = self._execution(lifecycle)
        return {
            "session_id": state.session_id,
            "lifecycle": lifecycle,
            "execution": execution,
            "queue_reason": queue_reason,
            "status": lifecycle if lifecycle != "open" else execution,
            "agent_status": agent_status,
            # Legacy API field, projected from the Run owner rather than a
            # second writable session-level task field.
            "user_goal": state.current_goal() if hasattr(state, "current_goal") else state.user_goal,
            "model": state.model_name,
            "interaction_mode": getattr(state, "interaction_mode", "agent"),
            "permission_mode": getattr(state, "permission_mode", None),
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

    def _execution(self, lifecycle: str) -> tuple[str, str | None]:
        if lifecycle != "open":
            return "idle", None
        coordinator = getattr(self.runtime, "directory_coordinator", None)
        if coordinator is not None:
            reason = coordinator.waiting_reason(self.session_id)
            if reason:
                return "queued", reason
        if self._interaction_snapshot():
            return "waiting_for_input", "waiting for permission or a reply"
        if not self.runtime.agent_idle.is_set():
            if self.runtime.cancellation_event.is_set():
                return "cancelling", "stopping the current turn"
            return "running", None
        return "idle", None

    def persist(self) -> None:
        """Save the session checkpoint. Adapters do not touch the store."""

        self._checkpoint()

    def list_saved_sessions(self, limit: int = 12) -> list[dict[str, Any]]:
        store = getattr(self.runtime, "checkpoint_store", None)
        if store is None:
            return []
        return list(store.list_recent_sessions(limit=limit))

    def history(self) -> list[dict[str, Any]]:
        from .history_projection import project_history, seed_ids

        state = self.runtime.session_state
        turn_ids, run_ids = seed_ids(state)
        return project_history(state, turn_ids, run_ids)

    def view(self) -> dict[str, Any]:
        """Stable status snapshot for a terminal or browser status line."""

        state = self.runtime.session_state
        summary = self.summary()
        drafts = getattr(self.runtime, "draft_attachments", None)
        pending = drafts.summaries() if drafts is not None else []
        limit = getattr(self.runtime.llm, "context_limit", None)
        return {
            **summary,
            "resumed": bool(getattr(self.runtime, "resumed", False)),
            "run_status": state.current_run_status() if hasattr(state, "current_run_status") else "",
            "context_tokens": getattr(state, "context_tokens", 0),
            "context_limit": limit,
            "workspace_dir": str(getattr(state, "workspace_dir", "") or ""),
            "plan_brief": _plan_brief(state),
            "staged_references": self.staged_references(),
            "draft_attachments": [
                {
                    "filename": record.filename,
                    "width": record.width,
                    "height": record.height,
                }
                for record in pending
            ],
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

    def activity(self) -> dict[str, Any]:
        """Canonical execution projection for every interface.

        Display widgets may fold or scroll this. They do not invent the status.
        """

        from ..workspace.timeline import (
            project_accessed_files,
            project_subagents,
            project_timeline,
        )

        snap = self.snapshot()
        state = self.runtime.session_state
        snap["timeline"] = project_timeline(state, snap["pending_interactions"])
        snap["subagents"] = project_subagents(state)
        snap["accessed_files"] = project_accessed_files(state)
        snap["plan"] = _plan_brief(state)
        snap["staged_references"] = self.staged_references()
        return snap

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
                item = self._queued.pop(command_id, None)
                cancelled = command_id in self._cancelled
                self._cancelled.discard(command_id)
                if not cancelled:
                    self._active_command = command_id
                    self._active_attachment_ids = {
                        str(attachment["id"])
                        for attachment in ((item or {}).get("attachments") or [])
                        if isinstance(attachment, dict) and attachment.get("id")
                    }
            if cancelled:
                store = getattr(self.runtime, "autonomy_store", None)
                if store is not None:
                    store.cancel_command(
                        self._command_scope, command_id, reason="queued instruction cancelled before start"
                    )
                self.publisher.publish(
                    "command.rejected",
                    {"command_id": command_id, "command": "turn.submit", "reason": "queued instruction cancelled before start"},
                )
                return False
            store = getattr(self.runtime, "autonomy_store", None)
            if store is not None:
                claimed = store.claim_command(
                    self._command_scope, command_id, self._consumer_id
                )
                if claimed is None:
                    return False
        try:
            stop = self._event_processor(self.runtime, event_type, payload)
            if command_id:
                try:
                    store = getattr(self.runtime, "autonomy_store", None)
                    if store is not None:
                        run = getattr(self.runtime.session_state, "active_run", lambda: None)()
                        result = {
                            "run_id": getattr(run, "run_id", ""),
                            "run_status": getattr(run, "status", "completed"),
                        }
                        status = "cancelled" if self.runtime.cancellation_event.is_set() else (
                            "completed" if result["run_status"] == "completed" else "failed"
                        )
                        store.complete_command(self._command_scope, command_id, result, status=status)
                except Exception:
                    logger.warning("could not persist command completion", exc_info=True)
            return stop
        except Exception as exc:
            if command_id:
                store = getattr(self.runtime, "autonomy_store", None)
                if store is not None:
                    try:
                        store.complete_command(
                            self._command_scope, command_id, {"error": str(exc)}, status="failed"
                        )
                    except Exception:
                        logger.warning("could not persist command failure", exc_info=True)
            raise
        finally:
            if command_id:
                with self._lock:
                    if self._active_command == command_id:
                        self._active_command = None
                        self._active_attachment_ids = set()

    @property
    def _event_processor(self) -> EventProcessor:
        # Stored on the runner closure through this property to keep tests able
        # to inject a fake processor without exposing UI implementation details.
        return self.__dict__["_processor"]

    @_event_processor.setter
    def _event_processor(self, value: EventProcessor) -> None:
        self.__dict__["_processor"] = value
