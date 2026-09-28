"""Multi-session runtime ownership for the local Web console."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...application.composition.host import ApplicationHost
from ...application.composition.runtime import (
    RuntimeConfig,
    WrightRuntime,
    assemble_runtime,
    shutdown_runtime,
)
from ...application.session.dispatch import process_session_event
from ...application.session.events import notice_text
from ...application.session.history_projection import (
    project_history,
    seed_ids,
)
from ...application.session.publisher import EventPublisher
from ...application.session.service import (
    SessionService,
    SessionServiceError,
)
from ...core.paths import project_id, session_dir, task_db_path
from ...domain.model.tool import ArtifactRef
from ...infrastructure.llm.model_adapters import available_models, process_model_name
from ...infrastructure.persistence.autonomy_store import (
    AutonomyNotFoundError,
    AutonomyStore,
)
from ...infrastructure.persistence.session.errors import CheckpointError
from ...infrastructure.persistence.session.repository import FileSessionRepository
from ...infrastructure.storage.attachments import AttachmentError, AttachmentRecord
from ...infrastructure.workspace.project import ProjectContext
from ...infrastructure.workspace.worktrees import ArchiveResult, WorktreeManager
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
            tool["artifacts"] = result["artifacts"]
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

    def __init__(self, runtime: WrightRuntime) -> None:
        self.runtime = runtime
        self.publisher = runtime.publisher
        self.interactions: InteractionBroker = runtime.interaction_broker
        self.service = SessionService(
            runtime,
            event_processor=process_session_event,
            shutdown=shutdown_runtime,
        )
        turn_ids, run_ids = seed_ids(runtime.session_state)

        def seed(_seq: int, _stream_id: str) -> None:
            self.publisher.display.seed(turn_ids, run_ids)

        self.publisher.capture(seed)
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
            queued_commands = [
                {
                    "command_id": item["command_id"],
                    "prompt": item["prompt"],
                    **({"attachments": item["attachments"]} if item.get("attachments") else {}),
                }
                for item in service_snapshot["queued_commands"]
            ]
            notices = [
                {
                    "id": event.event_id,
                    "type": event.type,
                    "text": notice_text(event.type, event.payload),
                    **(
                        {"kind": event.payload["kind"]}
                        if isinstance(event.payload.get("kind"), str)
                        else {}
                    ),
                }
                for event in self.publisher.retained_events()
                if event.type in {"system.notice", "system.checkpoint_error", "command.rejected"}
            ][-50:]
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
            }

        return self.publisher.capture(build)

    def _latest_user_attachments(self) -> list[dict[str, Any]]:
        records = getattr(self.runtime.session_state, "message_records", None)
        if not isinstance(records, list):
            return []
        for record in reversed(records):
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
        self.checkpoints = FileSessionRepository(session_dir(self.project_root))
        self._handles: dict[str, SessionHandle] = {}
        # Hosts outlive their creating browser session.  They are closed only
        # when the Web application itself stops (or an explicit host API is
        # introduced), never by SessionHandle.close().
        self._application_hosts: dict[Path, ApplicationHost] = {}
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

    def _runtime_config(
        self, *, context: ProjectContext, model: str | None, resume: str | None,
    ) -> RuntimeConfig:
        base = self.base_args
        hooks = getattr(base, "hooks_config", None)
        return RuntimeConfig(
            workspace=context.execution_root,
            resume=resume,
            continue_latest=False,
            no_session_persistence=bool(getattr(base, "no_session_persistence", False)),
            hooks_config=Path(hooks) if hooks else None,
            model=model,
            transport=getattr(base, "transport", None),
            trust_project_mcp=bool(getattr(base, "trust_project_mcp", False)),
            with_rag=bool(getattr(base, "with_rag", False)),
            mode=str(getattr(base, "mode", "coding") or "coding"),
        )

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
            retained_host = self._application_hosts.get(context.execution_root)
            try:
                runtime = assemble_runtime(
                    self._runtime_config(
                        context=context, model=model, resume=resume_session_id,
                    ),
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
            self._application_hosts[context.execution_root] = host
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

    def run_history(self, run_id: str) -> dict[str, Any]:
        """Project-level durable history, independent of a source Session."""
        with self._lock:
            hosts = tuple(self._application_hosts.values())
        for host in hosts:
            try:
                return host.run_history(run_id)
            except KeyError:
                continue
        # A browser may reconnect after the original host was stopped.  Read
        # the explicitly opened project DB without creating a scheduler or
        # acquiring an execution lock; no background work is started here.
        store = AutonomyStore(
            task_db_path(self.project_root),
            session_id="__web_history_query__",
            workspace_dir=self.project_root,
        )
        try:
            return store.run_history(run_id)
        except AutonomyNotFoundError as exc:
            raise RuntimeManagerError("durable run not found", status_code=404) from exc
        finally:
            store.close()

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
                if host.workspace_dir != context.execution_root:
                    continue
                if host.has_active_work():
                    raise RuntimeManagerError(
                        "worktree is still referenced by an active automation host"
                    )
                if not host.close():
                    raise RuntimeManagerError("worktree automation host is still closing")
                with self._lock:
                    self._application_hosts.pop(context.execution_root, None)
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
