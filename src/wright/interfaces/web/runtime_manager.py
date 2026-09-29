"""Multi-session runtime ownership for the local Web console."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...application.composition.runtime import (
    WrightRuntime,
    shutdown_runtime,
)
from ...application.session.directory import (
    SessionDirectory,
    SessionDirectoryError,
)
from ...application.session.dispatch import process_session_event
from ...application.session.events import notice_display, notice_text
from ...application.session.history_projection import (
    project_history,
    public_attachment,
    seed_ids,
)
from ...application.session.publisher import EventPublisher
from ...application.session.service import (
    SessionService,
    SessionServiceError,
)
from ...core.paths import project_id
from ...domain.model.tool import ArtifactRef
from ...infrastructure.storage.attachments import AttachmentError, AttachmentRecord
from ...infrastructure.workspace.worktrees import ArchiveResult
from ..interaction import InteractionBroker


def _tool_state(item: dict[str, Any]) -> dict[str, Any]:
    """Map a run tool record onto the frontend ToolState fields."""

    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    raw = str(item.get("phase") or item.get("status") or "planned")
    phase = {"pending": "planned", "timeout": "failed"}.get(raw, raw)
    if phase not in {"planned", "awaiting_approval", "running", "succeeded", "failed"}:
        phase = "failed"
    tool: dict[str, Any] = {
        "call_id": item.get("call_id"),
        "name": item.get("name") or item.get("tool_name") or "tool",
        "phase": phase,
    }
    if "arguments" in item:
        tool["arguments"] = item["arguments"]
    if result:
        if "ok" in result:
            tool["ok"] = result["ok"]
        if result.get("err"):
            tool["err"] = result["err"]
        if "data" in result:
            tool["data"] = result["data"]
        if result.get("artifacts"):
            tool["artifacts"] = [
                {key: value for key, value in artifact.items() if key != "storage_path"}
                if isinstance(artifact, dict) else artifact
                for artifact in result["artifacts"]
            ]
    return tool


class RuntimeManagerError(RuntimeError):
    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SessionHandle:
    """Web adapter over the shared session service.

    HTTP transfers bytes and projects a snapshot. Attachment validation and
    session commands are delegated to ``SessionService``.
    """

    def __init__(self, runtime: WrightRuntime | None = None, *, opened: Any = None) -> None:
        if opened is not None:
            self.runtime = opened.runtime
            self.publisher = opened.publisher
            self.interactions = opened.interactions
            self.service = opened.service
        else:
            if runtime is None:
                raise RuntimeManagerError("session handle requires a runtime")
            self.runtime = runtime
            self.publisher = runtime.publisher
            self.interactions = runtime.interaction_broker
            self.service = SessionService(
                runtime,
                event_processor=process_session_event,
                shutdown=shutdown_runtime,
            )
            self.service.start()
        turn_ids, run_ids = seed_ids(self.runtime.session_state)

        def seed(_seq: int, _stream_id: str) -> None:
            self.publisher.display.seed(turn_ids, run_ids)

        self.publisher.capture(seed)

    @property
    def session_id(self) -> str:
        return self.runtime.session_state.session_id

    @property
    def closed(self) -> bool:
        return self.service.closed

    @property
    def thread(self):
        return self.service.runner.thread

    def _attachment_summaries(self, attachment_ids: list[str]) -> list[dict[str, object]]:
        if not attachment_ids:
            return []
        try:
            return [
                public_attachment(record)
                for record in self.runtime.session_state.attachment_records(attachment_ids)
            ]
        except (AttributeError, ValueError) as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def upload_attachment(self, filename: str, data: bytes) -> dict[str, object]:
        try:
            return self.service.add_attachment(self.session_id, filename, data)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc), status_code=409) from exc

    def remove_attachment(self, attachment_id: str) -> None:
        try:
            self.service.remove_attachment(self.session_id, attachment_id)
        except SessionServiceError as exc:
            status = 404 if "not found" in str(exc) else 409
            raise RuntimeManagerError(str(exc), status_code=status) from exc

    def attachment_path(self, attachment_id: str) -> tuple[AttachmentRecord, Path]:
        record = self.runtime.session_state.attachments.get(attachment_id)
        if record is None:
            raise RuntimeManagerError("attachment not found", status_code=404)
        try:
            return record, self.runtime.attachment_store.path_for(record)
        except AttachmentError as exc:
            raise RuntimeManagerError(str(exc), status_code=404) from exc

    def attachment_thumbnail_path(self, attachment_id: str) -> tuple[AttachmentRecord, Path]:
        record = self.runtime.session_state.attachments.get(attachment_id)
        if record is None:
            raise RuntimeManagerError("attachment not found", status_code=404)
        try:
            return record, self.runtime.attachment_store.thumbnail_path_for(record)
        except AttachmentError as exc:
            raise RuntimeManagerError(str(exc), status_code=404) from exc

    def artifact_path(self, artifact_id: str) -> tuple[ArtifactRef, Path]:
        """Resolve only references recorded in this Session's tool history."""
        for execution in self.runtime.session_state.tool_executions.values():
            result = execution.result
            if result is None:
                continue
            for ref in result.artifacts:
                if ref.id == artifact_id:
                    try:
                        return ref, self.runtime.artifact_store.path_for(ref)
                    except (FileNotFoundError, ValueError) as exc:
                        raise RuntimeManagerError(str(exc), status_code=404) from exc
        raise RuntimeManagerError("artifact not found in this session", status_code=404)

    def submit(
        self, prompt: str, command_id: str, attachment_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not command_id:
            raise RuntimeManagerError("command_id is required")
        try:
            return self.service.submit(
                prompt, command_id, attachment_ids, document_ids, references,
            )
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def cancel(self, command_id: str) -> dict[str, Any]:
        """Stop the current turn and leave queued messages in place."""

        if not command_id:
            raise RuntimeManagerError("command_id is required")
        try:
            return self.service.stop_current(command_id)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def cancel_all(self, command_id: str) -> dict[str, Any]:
        """Stop the current turn and cancel every queued message."""

        if not command_id:
            raise RuntimeManagerError("command_id is required")
        try:
            return self.service.stop_all(command_id)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def cancel_queued(self, command_id: str, target_command_id: str) -> dict[str, Any]:
        try:
            return self.service.cancel_queued(command_id, target_command_id)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def respond(self, command_id: str, request_id: str, answer: Any) -> dict[str, Any]:
        try:
            return self.service.respond_interaction(command_id, request_id, answer)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def command_status(self, command_id: str) -> dict[str, Any]:
        try:
            return self.service.command_status(command_id)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc), status_code=404) from exc

    def snapshot(self) -> dict[str, Any]:
        """Project the view at one sequence watermark.

        ``capture`` holds the publisher lock only while copying state. The
        returned ``last_seq`` is that watermark, not a later read.
        """

        def build(seq: int, stream_id: str) -> dict[str, Any]:
            state = self.runtime.session_state
            service_snapshot = self.service.snapshot()
            view = self.publisher.display.view()
            active = view["active_turn"]
            authoritative_run = service_snapshot["active_run"]
            if active is None and authoritative_run is not None:
                active = {
                    "run_id": authoritative_run["run_id"],
                    "turn_id": None,
                    "prompt": authoritative_run["goal"],
                    "attachments": self._latest_user_attachments(),
                    "reasoning": "",
                    "content": "",
                    "tools": [
                        _tool_state(item) for item in authoritative_run["tools"]
                    ],
                }
            elif active is not None and not active.get("tools") and authoritative_run is not None:
                active = {
                    **active,
                    "tools": [_tool_state(item) for item in authoritative_run["tools"]],
                }
            usage = state.task_usage()
            request = view["request_usage"]
            from ...application.workspace.context_usage import empty_context
            from ...application.workspace.timeline import (
                project_accessed_files,
                project_subagents,
                project_timeline,
            )

            breakdown = getattr(state, "request_context_breakdown", None)
            if not isinstance(breakdown, dict) or not breakdown:
                breakdown = empty_context(getattr(self.runtime.llm, "context_limit", None))
            queued_commands = [
                {
                    "command_id": item["command_id"],
                    "prompt": item["prompt"],
                    **({"attachments": item["attachments"]} if item.get("attachments") else {}),
                }
                for item in service_snapshot["queued_commands"]
            ]
            notices = []
            for event in self.publisher.retained_events():
                if event.type not in {"system.notice", "system.checkpoint_error", "command.rejected"}:
                    continue
                code, params = notice_display(event.type, event.payload)
                item = {
                    "id": event.event_id,
                    "type": event.type,
                    "text": notice_text(event.type, event.payload),
                }
                if isinstance(event.payload.get("kind"), str):
                    item["kind"] = event.payload["kind"]
                if code:
                    item["code"] = code
                    item["params"] = params
                notices.append(item)
            notices = notices[-50:]
            return {
                "stream_id": stream_id,
                "last_seq": seq,
                "session": self.summary(),
                "history": project_history(
                    state,
                    set(self.publisher.display.published_turns),
                    set(self.publisher.display.published_runs),
                ),
                "active_turn": active,
                "agents": view["agents"],
                "plan": state.plan_manager.snapshot(),
                "pending_interactions": service_snapshot["pending_interactions"],
                "notices": notices,
                "queued_commands": queued_commands,
                "queue_depth": len(queued_commands),
                "usage": {
                    "kind": "provider_billing",
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "request_prompt_tokens": None if request is None else request.get("prompt_tokens"),
                    "request_completion_tokens": None if request is None else request.get("completion_tokens"),
                    "request_total_tokens": None if request is None else request.get("total_tokens"),
                    "context_tokens": (
                        view["context_tokens"]
                        if view["context_tokens"] is not None
                        else getattr(state, "request_context_tokens", None)
                    ),
                    "context_limit": (
                        view["context_limit"]
                        if view["context_limit"] is not None
                        else self.runtime.llm.context_limit
                    ),
                },
                "context_breakdown": breakdown,
                "timeline": project_timeline(
                    state, service_snapshot["pending_interactions"],
                ),
                "subagents": project_subagents(state),
                "accessed_files": project_accessed_files(state),
            }

        return self.publisher.capture(build)

    def _latest_user_attachments(self) -> list[dict[str, Any]]:
        records = getattr(self.runtime.session_state, "message_records", None)
        if not isinstance(records, list):
            return []
        for record in reversed(records):
            if getattr(record, "source", "") != "user_input":
                continue
            message = record.message
            if message.get("role") != "user":
                continue
            attachment_ids = message.get("attachments") or []
            if isinstance(attachment_ids, list) and attachment_ids:
                return self._attachment_summaries([str(item) for item in attachment_ids])
        return []

    def summary(self) -> dict[str, Any]:
        return {**self.service.summary(), "recoverable": True}

    def set_model(self, model: str) -> dict[str, Any]:
        try:
            self.service.set_model(model)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc
        return self.summary()

    def set_execution_policy(
        self,
        *,
        interaction_mode: str | None = None,
        permission_mode: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.service.set_execution_policy(
                interaction_mode=interaction_mode,
                permission_mode=permission_mode,
            )
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc), status_code=409) from exc

    def upload_document(self, filename: str, data: bytes) -> dict[str, Any]:
        try:
            return self.service.add_document(self.session_id, filename, data)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc), status_code=409) from exc

    def close(self) -> bool:
        return self.service.close(wait_timeout=5)


class RuntimeManager:
    """HTTP adapter over SessionDirectory.

    This object maps directory failures onto web errors. Session lifetime,
    capacity, worktrees, and ApplicationHost retention live in the directory.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        capacity: int = 4,
        base_args: argparse.Namespace,
        directory: SessionDirectory | None = None,
    ) -> None:
        self.directory = directory or SessionDirectory(
            project_root, capacity=capacity, base_args=base_args,
        )
        self.project_root = self.directory.project_root
        self.capacity = self.directory.capacity
        self.base_args = base_args
        self._handles: dict[str, SessionHandle] = {}
        self._lock = threading.RLock()

    def project(self) -> dict[str, Any]:
        return self.directory.project()

    def set_model(self, session_id: str, model: str) -> dict[str, Any]:
        return self.get(session_id).set_model(model)

    def create(
        self,
        *,
        environment: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        resume: str | None = None,
        continue_latest: bool = False,
        client_request_id: str | None = None,
        command_id: str | None = None,
        references: list[dict[str, Any]] | None = None,
        attachment_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        interaction_mode: str | None = None,
        permission_mode: str | None = None,
    ) -> SessionHandle:
        return self._open_on(
            self.directory,
            environment=environment,
            model=model,
            prompt=prompt,
            resume=resume,
            continue_latest=continue_latest,
            client_request_id=client_request_id,
            command_id=command_id,
            references=references,
            attachment_ids=attachment_ids,
            document_ids=document_ids,
            interaction_mode=interaction_mode,
            permission_mode=permission_mode,
        )

    def _open_on(
        self,
        directory: SessionDirectory,
        *,
        environment: str | None,
        model: str | None,
        prompt: str | None,
        resume: str | None,
        continue_latest: bool = False,
        client_request_id: str | None = None,
        command_id: str | None = None,
        references: list[dict[str, Any]] | None = None,
        attachment_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        interaction_mode: str | None = None,
        permission_mode: str | None = None,
    ) -> SessionHandle:
        session_id = resume or uuid4().hex[:12]
        publisher = EventPublisher(
            project_id=project_id(directory.project_root), session_id=session_id,
        )
        broker = InteractionBroker(publisher)
        try:
            opened = directory.open(
                environment=environment,
                model=model,
                resume=resume,
                continue_latest=continue_latest,
                interaction_broker=broker,
                publisher=publisher,
                session_id=None if resume or continue_latest else session_id,
                client_request_id=client_request_id,
            )
        except SessionDirectoryError as exc:
            broker.close()
            publisher.close()
            if exc.kind == "not_found":
                status = 404
            elif exc.kind == "history_only":
                status = 409
            else:
                status = None
            raise RuntimeManagerError(str(exc), status_code=status) from exc
        except Exception:
            broker.close()
            publisher.close()
            raise
        if opened.publisher is not publisher:
            broker.close()
            publisher.close()
        handle = SessionHandle(opened=opened)
        handle.owner = directory
        handle.submit_error = None
        with self._lock:
            self._handles[handle.session_id] = handle
        if (interaction_mode or permission_mode) and not resume and not continue_latest:
            try:
                handle.set_execution_policy(
                    interaction_mode=interaction_mode,
                    permission_mode=permission_mode,
                )
            except RuntimeManagerError as exc:
                handle.submit_error = str(exc)
        publisher.publish("session.snapshot", handle.snapshot())
        has_turn = bool((prompt and prompt.strip()) or references or attachment_ids or document_ids)
        if has_turn and handle.submit_error is None:
            try:
                handle.submit(
                    prompt or "",
                    command_id or uuid4().hex,
                    attachment_ids,
                    document_ids,
                    references,
                )
            except RuntimeManagerError as exc:
                handle.submit_error = str(exc)
        return handle

    def get(self, session_id: str) -> SessionHandle:
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is None or handle.closed:
            raise RuntimeManagerError("session not found", status_code=404)
        return handle

    def run_history(self, run_id: str) -> dict[str, Any]:
        try:
            return self.directory.run_history(run_id)
        except SessionDirectoryError as exc:
            status = 404 if exc.kind == "not_found" else None
            raise RuntimeManagerError(str(exc), status_code=status) from exc

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.directory.list_sessions()

    def close(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            handle = self._handles.get(session_id)
        directory = getattr(handle, "owner", None) or self._ui_directory()
        if handle is None:
            return directory.close(session_id)
        summary = directory.close(session_id)
        if handle.closed:
            with self._lock:
                self._handles.pop(session_id, None)
        return summary

    def archive(self, session_id: str) -> ArchiveResult:
        with self._lock:
            handle = self._handles.get(session_id)
        directory = getattr(handle, "owner", None) or self._ui_directory()
        try:
            return directory.archive(session_id)
        except SessionDirectoryError as exc:
            raise RuntimeManagerError(str(exc)) from exc
        finally:
            with self._lock:
                handle = self._handles.get(session_id)
                if handle is not None and handle.closed:
                    self._handles.pop(session_id, None)

    def shutdown(self) -> None:
        with self._lock:
            self._handles.clear()
            extras = list(getattr(self, "_directories", {}).values())
            self._directories = {}
        for directory in extras:
            directory.shutdown()
        self.directory.shutdown()

    def _catalog(self):
        from ...application.workspace.catalog import WorkspaceCatalog

        catalog = getattr(self, "_workspace_catalog", None)
        if catalog is None:
            catalog = WorkspaceCatalog()
            self._workspace_catalog = catalog
        return catalog

    def workspaces(self) -> dict[str, Any]:
        return self._catalog().bootstrap(self.project_root)

    def register_workspace(self, root: str) -> dict[str, Any]:
        from ...application.workspace.catalog import WorkspaceCatalogError

        try:
            return self._catalog().register(Path(root))
        except WorkspaceCatalogError as exc:
            raise RuntimeManagerError(str(exc), status_code=400) from exc

    def unregister_workspace(self, project_id: str) -> dict[str, Any]:
        from ...application.workspace.catalog import WorkspaceCatalogError

        try:
            return self._catalog().unregister(project_id)
        except WorkspaceCatalogError as exc:
            raise RuntimeManagerError(str(exc), status_code=404) from exc

    def select_workspace(self, project_id: str) -> dict[str, Any]:
        """Record the UI selection. Open sessions keep their execution roots."""

        from ...application.workspace.catalog import WorkspaceCatalogError

        try:
            selected = self._catalog().select(project_id)
        except WorkspaceCatalogError as exc:
            raise RuntimeManagerError(str(exc), status_code=404) from exc
        return {**selected, "running_sessions_unchanged": True}

    def _directory_for(self, registered_id: str) -> SessionDirectory:
        from ...application.workspace.catalog import WorkspaceCatalogError

        if registered_id == project_id(self.project_root):
            return self.directory
        try:
            record = self._catalog().get(registered_id)
        except WorkspaceCatalogError as exc:
            raise RuntimeManagerError(str(exc), status_code=404) from exc
        root = Path(str(record["root"]))
        with self._lock:
            directories = getattr(self, "_directories", None)
            if directories is None:
                directories = {}
                self._directories = directories
            found = directories.get(registered_id)
            if found is None:
                found = SessionDirectory(root, capacity=self.capacity, base_args=self.base_args)
                directories[registered_id] = found
            return found

    def _ui_directory(self) -> SessionDirectory:
        selected = self.workspaces().get("selected_project_id")
        if isinstance(selected, str) and selected:
            return self._directory_for(selected)
        return self.directory

    def workspace_sessions(self, registered_id: str) -> list[dict[str, Any]]:
        return self._directory_for(registered_id).list_sessions()

    def preview(self, session_id: str) -> dict[str, Any]:
        try:
            return self._ui_directory().preview(session_id)
        except SessionDirectoryError as exc:
            try:
                return self.directory.preview(session_id)
            except SessionDirectoryError:
                status = 404 if exc.kind == "not_found" else None
                raise RuntimeManagerError(str(exc), status_code=status) from exc

    def relabel(self, session_id: str, label: str) -> dict[str, Any]:
        with self._lock:
            handle = self._handles.get(session_id)
        directory = getattr(handle, "owner", None) or self._ui_directory()
        try:
            return directory.relabel(session_id, label)
        except SessionDirectoryError as exc:
            status = 404 if exc.kind == "not_found" else 400
            raise RuntimeManagerError(str(exc), status_code=status) from exc

    def search_references(self, query: str) -> list[dict[str, Any]]:
        return self.directory.search_references(query)

    def create_in_workspace(self, registered_id: str, **fields: Any) -> SessionHandle:
        directory = self._directory_for(registered_id)
        return self._open_on(directory, **fields)
