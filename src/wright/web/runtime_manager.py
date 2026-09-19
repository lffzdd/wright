"""Multi-session runtime ownership for the local Web console."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..application_host import ApplicationHost
from ..attachments import AttachmentError, AttachmentRecord
from ..checkpoint import CheckpointError, SessionCheckpointStore
from ..interaction import InteractionBroker
from ..paths import project_id, session_dir
from ..project import ProjectContext
from ..renderer import SilentRenderer
from ..runtime import (
    WrightRuntime,
    assemble_runtime,
    runtime_config_from_args,
    shutdown_runtime,
)
from ..session_host import process_session_event
from ..session_models import available_models, process_model_name
from ..session_service import SessionService, SessionServiceError
from ..tools.base import ArtifactRef
from ..ui_events import EventPublisher
from ..worktrees import ArchiveResult, WorktreeManager


class RuntimeManagerError(RuntimeError):
    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SessionHandle:
    """Web adapter over the shared session service.

    Attachment transport and HTTP snapshot projection are Web concerns; all
    session commands themselves are delegated to ``SessionService``.
    """

    def __init__(self, runtime: WrightRuntime) -> None:
        self.runtime = runtime
        self.publisher = runtime.publisher
        self.interactions: InteractionBroker = runtime.interaction_broker
        self.service = SessionService(
            runtime,
            event_processor=process_session_event,
            shutdown=shutdown_runtime,
        )
        self.service.start()

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
            return [record.to_dict() for record in self.runtime.session_state.attachment_records(attachment_ids)]
        except (AttributeError, ValueError) as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def upload_attachment(self, filename: str, data: bytes) -> dict[str, object]:
        if self.closed:
            raise RuntimeManagerError("session is closed")
        try:
            record = self.runtime.attachment_store.register_bytes(
                filename, data, self.runtime.session_state.attachments
            )
            self.runtime.session_state.attachments[record.id] = record
            store = getattr(self.runtime.agent, "checkpoint_store", None)
            if store is not None:
                store.save(self.runtime.session_state)
            return record.to_dict()
        except AttachmentError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def remove_attachment(self, attachment_id: str) -> None:
        record = self.runtime.session_state.attachments.get(attachment_id)
        if record is None:
            raise RuntimeManagerError("attachment not found", status_code=404)
        if any(
            attachment_id in (message.message.get("attachments") or [])
            for message in self.runtime.session_state.message_records
        ):
            raise RuntimeManagerError("attachment is already part of conversation history")
        self.runtime.attachment_store.remove(record)
        del self.runtime.session_state.attachments[attachment_id]
        store = getattr(self.runtime.agent, "checkpoint_store", None)
        if store is not None:
            store.save(self.runtime.session_state)

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
    ) -> dict[str, Any]:
        if not command_id:
            raise RuntimeManagerError("command_id is required")
        try:
            return self.service.submit(prompt, command_id, attachment_ids)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def cancel(self, command_id: str) -> dict[str, Any]:
        if not command_id:
            raise RuntimeManagerError("command_id is required")
        try:
            return self.service.cancel_current(command_id, cancel_queued=True)
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
        state = self.runtime.session_state
        service_snapshot = self.service.snapshot()
        authoritative_run = service_snapshot["active_run"]
        active: dict[str, Any] | None = None
        if authoritative_run is not None:
            # This live projection, not the finite UI-event ring, owns stream
            # accumulation. Reconnection therefore does not require the old
            # turn.started/content.delta events to still be retained.
            response = self.runtime.runtime_resources.response_snapshot(
                authoritative_run["run_id"]
            ) or {}
            active = {
                "run_id": authoritative_run["run_id"],
                "turn_id": None,
                "prompt": authoritative_run["goal"],
                "attachments": [],
                "reasoning": response.get("reasoning", ""),
                "content": response.get("content", ""),
                "tools": response.get("tools") or authoritative_run["tools"],
            }
        usage = state.task_usage()
        queued_commands = [
            {
                "command_id": item["command_id"],
                "prompt": item["prompt"],
                **({"attachments": item["attachments"]} if item.get("attachments") else {}),
            }
            for item in service_snapshot["queued_commands"]
        ]
        return {
            "stream_id": self.publisher.stream_id,
            "last_seq": self.publisher.latest_seq,
            "session": self.summary(),
            "history": self._history(),
            "active_turn": active,
            "plan": state.plan_manager.snapshot(),
            "pending_interactions": service_snapshot["pending_interactions"],
            "notices": [
                {"id": event.event_id, "type": event.type, **event.payload}
                for event in self.publisher.retained_events()
                if event.type in {"system.notice", "system.checkpoint_error", "command.rejected"}
            ][-50:],
            "queued_commands": queued_commands,
            "queue_depth": len(queued_commands),
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "request_prompt_tokens": 0,
                "request_completion_tokens": 0,
                "request_total_tokens": 0,
                "context_tokens": getattr(
                    state, "request_context_tokens", state.context_tokens
                ),
                "context_limit": self.runtime.llm.context_limit,
            },
        }

    def _history(self) -> list[dict[str, Any]]:
        """Project final turns with the image references of their user turn."""
        records = getattr(self.runtime.session_state, "message_records", None)
        if not isinstance(records, list):
            return []
        id_to_index = {record.id: index for index, record in enumerate(records)}
        history: list[dict[str, Any]] = []
        for turn in self.runtime.session_state.turns:
            if turn.route != "final":
                continue
            answer = turn.parsed.get("final_answer", "")
            if not isinstance(answer, str):
                answer = str(answer)
            if not answer.strip():
                continue
            user: dict[str, Any] | None = None
            for index in range(id_to_index.get(turn.message_id, 0) - 1, -1, -1):
                candidate = records[index].message
                if candidate.get("role") != "user":
                    continue
                text = candidate.get("content", "")
                if not isinstance(text, str) or text.lstrip().startswith("<"):
                    continue
                user = candidate
                break
            if user is None:
                continue
            attachment_ids = user.get("attachments", [])
            attachments = self._attachment_summaries(attachment_ids) if isinstance(attachment_ids, list) else []
            item: dict[str, Any] = {
                "user": str(user.get("content", "")).strip(),
                "assistant": answer.strip(),
            }
            if attachments:
                item["attachments"] = attachments
            # The Run owns tool executions.  Include its completed calls in
            # this history projection so artifact links survive event-cache
            # eviction and a resumed session can still display them.
            run = self.runtime.session_state.runs.get(turn.run_id)
            if run is not None:
                tools: list[dict[str, Any]] = []
                for call_id in run.tool_execution_ids:
                    execution = self.runtime.session_state.tool_executions.get(call_id)
                    if execution is None:
                        continue
                    tool: dict[str, Any] = {
                        "call_id": execution.call.id,
                        "name": execution.call.name,
                        "arguments": dict(execution.call.arguments),
                        "phase": execution.status,
                    }
                    if execution.result is not None:
                        tool.update(execution.result.to_dict())
                    tools.append(tool)
                if tools:
                    item["tools"] = tools
            if history and history[-1]["user"] == item["user"]:
                history[-1] = item
            else:
                history.append(item)
        return history

    def summary(self) -> dict[str, Any]:
        return {**self.service.summary(), "recoverable": True}

    def set_model(self, model: str) -> dict[str, Any]:
        try:
            self.service.set_model(model)
        except SessionServiceError as exc:
            raise RuntimeManagerError(str(exc)) from exc
        return self.summary()

    def close(self) -> bool:
        return self.service.close(wait_timeout=5)


class RuntimeManager:
    def __init__(self, project_root: Path, *, capacity: int = 4, base_args: argparse.Namespace) -> None:
        if capacity <= 0:
            raise ValueError("web capacity must be positive")
        self.project_root = project_root.expanduser().resolve()
        if not self.project_root.is_dir():
            raise RuntimeManagerError(f"workspace does not exist: {self.project_root}")
        self.capacity = capacity
        self.base_args = base_args
        self.worktrees = WorktreeManager(self.project_root)
        self.checkpoints = SessionCheckpointStore(session_dir(self.project_root))
        self._handles: dict[str, SessionHandle] = {}
        # Hosts outlive their creating browser session.  They are closed only
        # when the Web application itself stops (or an explicit host API is
        # introduced), never by SessionHandle.close().
        self._application_hosts: dict[str, ApplicationHost] = {}
        self._lock = threading.RLock()

    def project(self) -> dict[str, Any]:
        base_model = process_model_name(getattr(self.base_args, "model", None))
        models = list(available_models(base_model))
        return {
            "project_id": project_id(self.project_root),
            "name": self.project_root.name,
            "project_root": str(self.project_root),
            "git": self.worktrees.is_git,
            "capacity": self.capacity,
            "active_count": len(self._handles),
            "default_environment": "worktree" if self.worktrees.is_git else "local",
            "dirty_checkout": bool(
                self.worktrees.is_git
                and self.worktrees.inspect(ProjectContext.local(self.project_root))["dirty"]
            ),
            "default_model": base_model,
            "models": models,
        }

    def set_model(self, session_id: str, model: str) -> dict[str, Any]:
        with self._lock:
            handle = self._handles.get(session_id)
            if handle is None:
                raise RuntimeManagerError(
                    f"active session not found: {session_id}",
                    status_code=404,
                )
            return handle.set_model(model)

    def _args(self, *, context: ProjectContext, model: str | None, resume: str | None) -> argparse.Namespace:
        values = vars(self.base_args).copy()
        values.update({
            "workspace": context.execution_root,
            "model": model,
            "resume": resume,
            "continue_latest": False,
            "ui": "web",
        })
        return argparse.Namespace(**values)

    def create(
        self,
        *,
        environment: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        resume_session_id: str | None = None,
    ) -> SessionHandle:
        with self._lock:
            if len(self._handles) >= self.capacity:
                raise RuntimeManagerError(f"web capacity {self.capacity} reached")
            if resume_session_id and resume_session_id in self._handles:
                raise RuntimeManagerError("session is already active")
            if resume_session_id:
                try:
                    saved = self.checkpoints.load(resume_session_id)
                except CheckpointError as exc:
                    raise RuntimeManagerError(str(exc)) from exc
                saved_project = (saved.project_root or self.project_root).resolve()
                if saved_project != self.project_root:
                    raise RuntimeManagerError("checkpoint belongs to a different project")
                context = ProjectContext(
                    project_root=saved_project,
                    execution_root=saved.workspace_dir,
                    environment=saved.environment,
                    base_commit=saved.base_commit,
                    branch_name=saved.branch_name,
                )
                session_id = resume_session_id
            else:
                session_id = uuid4().hex[:12]
                selected = environment or ("worktree" if self.worktrees.is_git else "local")
                if selected not in {"local", "worktree"}:
                    raise RuntimeManagerError("environment must be local or worktree")
                if selected == "worktree" and not self.worktrees.is_git:
                    selected = "local"
                if selected == "local" and any(
                    item.runtime.project_context.environment == "local"
                    for item in self._handles.values()
                ):
                    raise RuntimeManagerError("local checkout already has an active session")
                context = (
                    self.worktrees.create(session_id)
                    if selected == "worktree"
                    else ProjectContext.local(self.project_root)
                )
            if context.environment == "local" and any(
                item.runtime.project_context.environment == "local"
                for item in self._handles.values()
            ):
                raise RuntimeManagerError("local checkout already has an active session")
            publisher = EventPublisher(
                project_id=project_id(self.project_root), session_id=session_id
            )
            broker = InteractionBroker(publisher)
            retained_host = (
                self._application_hosts.get(session_id)
                if resume_session_id else None
            )
            try:
                runtime = assemble_runtime(
                    runtime_config_from_args(self._args(context=context, model=model, resume=resume_session_id)),
                    renderer=SilentRenderer(),
                    project_context=context,
                    publisher=publisher,
                    interaction_broker=broker,
                    session_id=session_id,
                    application_host=retained_host,
                )
            except Exception:
                broker.close()
                publisher.close()
                if not resume_session_id and context.environment == "worktree":
                    self.worktrees.archive(context)
                raise
            host = runtime.application_host
            if host is None:
                shutdown_runtime(runtime)
                raise RuntimeManagerError("runtime did not construct ApplicationHost")
            runtime.owns_application_host = False
            handle = SessionHandle(runtime)
            self._handles[session_id] = handle
            self._application_hosts[session_id] = host
        publisher.publish("session.snapshot", handle.snapshot())
        if prompt and prompt.strip():
            handle.submit(prompt, uuid4().hex)
        return handle

    def get(self, session_id: str) -> SessionHandle:
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is None:
            raise RuntimeManagerError("session is not active")
        return handle

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            active = {key: value.summary() for key, value in self._handles.items()}
        results = list(active.values())
        for saved in self.checkpoints.list_recent_sessions(limit=100):
            if saved["session_id"] in active:
                continue
            results.append({**saved, "active": False, "status": "closed"})
        return results

    def close(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is None:
            raise RuntimeManagerError("session is not active")
        finished = handle.close()
        summary = handle.summary()
        if finished:
            with self._lock:
                self._handles.pop(session_id, None)
        return summary

    def archive(self, session_id: str) -> ArchiveResult:
        try:
            handle = self.get(session_id)
        except RuntimeManagerError:
            saved = self.checkpoints.load(session_id)
            context = ProjectContext(
                project_root=saved.project_root or self.project_root,
                execution_root=saved.workspace_dir,
                environment=saved.environment,
                base_commit=saved.base_commit,
                branch_name=saved.branch_name,
            )
        else:
            context = handle.runtime.project_context
            self.close(session_id)
        if context.environment == "worktree":
            with self._lock:
                hosts = tuple(self._application_hosts.values())
            for host in hosts:
                if host.workspace_dir != context.execution_root or host.state != "running":
                    continue
                if host.store.count_active_runs() or any(
                    automation.status == "active"
                    for automation in host.store.list_automations()
                ):
                    raise RuntimeManagerError(
                        "worktree is still referenced by an active automation host"
                    )
        return self.worktrees.archive(context)

    def shutdown(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
            hosts = list(self._application_hosts.values())
            self._application_hosts.clear()
        for handle in handles:
            handle.close()
        for host in hosts:
            host.close()
