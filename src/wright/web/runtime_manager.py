"""Multi-session runtime ownership for the local Web console."""

from __future__ import annotations

import argparse
import threading
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..checkpoint import CheckpointError, SessionCheckpointStore
from ..interaction import InteractionBroker
from ..paths import project_id, session_dir
from ..project import ProjectContext
from ..renderer import SilentRenderer, collect_history_pairs
from ..runtime import WrightRuntime, build_runtime, shutdown_runtime
from ..session_host import process_session_event
from ..ui_events import EventPublisher
from ..worktrees import ArchiveResult, WorktreeManager


class RuntimeManagerError(RuntimeError):
    pass


class SessionHandle:
    def __init__(self, runtime: WrightRuntime) -> None:
        self.runtime = runtime
        self.publisher = runtime.publisher
        self.interactions: InteractionBroker = runtime.interaction_broker
        self._closed = threading.Event()
        self._commands: set[str] = set()
        self._command_order: deque[str] = deque(maxlen=2_000)
        self._command_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._worker,
            name=f"wright-web-{runtime.session_state.session_id}",
            daemon=True,
        )
        self.thread.start()

    @property
    def session_id(self) -> str:
        return self.runtime.session_state.session_id

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _worker(self) -> None:
        event_queue = self.runtime.event_queue
        while not self._closed.is_set():
            event_type, payload = event_queue.get()
            try:
                if process_session_event(self.runtime, event_type, payload):
                    return
            except Exception as exc:
                self.runtime.agent_idle.set()
                self.publisher.publish(
                    "system.notice", {"text": f"session worker error: {exc}"}
                )

    def _remember_command(self, command_id: str) -> bool:
        with self._command_lock:
            if command_id in self._commands:
                return False
            if len(self._command_order) == self._command_order.maxlen:
                oldest = self._command_order.popleft()
                self._commands.discard(oldest)
            self._command_order.append(command_id)
            self._commands.add(command_id)
            return True

    def submit(self, prompt: str, command_id: str) -> dict[str, Any]:
        if self.closed:
            raise RuntimeManagerError("session is closed")
        cleaned = prompt.strip()
        if not cleaned:
            raise RuntimeManagerError("prompt cannot be empty")
        if not command_id:
            raise RuntimeManagerError("command_id is required")
        fresh = self._remember_command(command_id)
        queued = not self.runtime.agent_idle.is_set() or not self.runtime.event_queue.empty()
        if fresh:
            self.runtime.event_queue.put((
                "USER_INPUT",
                {"prompt": cleaned, "command_id": command_id},
            ))
        event = self.publisher.publish(
            "command.accepted",
            {"command_id": command_id, "command": "turn.submit", "queued": queued, "duplicate": not fresh},
        )
        return event.to_dict()

    def cancel(self, command_id: str) -> dict[str, Any]:
        if not command_id:
            raise RuntimeManagerError("command_id is required")
        fresh = self._remember_command(command_id)
        if fresh:
            self.runtime.cancellation_event.set()
            self.interactions.cancel_pending()
        event = self.publisher.publish(
            "command.accepted",
            {"command_id": command_id, "command": "turn.cancel", "duplicate": not fresh},
        )
        return event.to_dict()

    def respond(self, command_id: str, request_id: str, answer: Any) -> dict[str, Any]:
        if not command_id:
            raise RuntimeManagerError("command_id is required")
        fresh = self._remember_command(command_id)
        resolved = self.interactions.resolve(request_id, answer) if fresh else False
        event_type = "command.accepted" if resolved or not fresh else "command.rejected"
        event = self.publisher.publish(event_type, {
            "command_id": command_id,
            "command": "interaction.respond",
            "request_id": request_id,
            "duplicate": not fresh,
            "reason": "" if resolved or not fresh else "interaction is no longer pending",
        })
        return event.to_dict()

    def snapshot(self) -> dict[str, Any]:
        state = self.runtime.session_state
        active: dict[str, Any] | None = None
        for event in self.publisher.retained_events():
            if event.type == "turn.started":
                active = {
                    "turn_id": event.turn_id,
                    "prompt": event.payload.get("prompt", ""),
                    "reasoning": "",
                    "content": "",
                    "tools": {},
                }
            elif active is not None and event.turn_id in {None, active["turn_id"]}:
                if event.type == "reasoning.delta":
                    active["reasoning"] += str(event.payload.get("piece", ""))
                elif event.type == "content.delta":
                    active["content"] += str(event.payload.get("piece", ""))
                elif event.type == "content.final":
                    active["content"] = event.payload.get("content", "")
                elif event.type == "tool.started":
                    active["tools"][event.payload.get("call_id", "")] = dict(event.payload)
                elif event.type == "tool.output":
                    tool = active["tools"].setdefault(event.payload.get("call_id", ""), {})
                    tool["output"] = tool.get("output", "") + str(event.payload.get("output", ""))
                elif event.type == "tool.finished":
                    active["tools"].setdefault(event.payload.get("call_id", ""), {}).update(event.payload)
                elif event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                    active = None
        if active is not None:
            active["tools"] = list(active["tools"].values())
        usage = state.task_usage()
        return {
            "stream_id": self.publisher.stream_id,
            "last_seq": self.publisher.latest_seq,
            "session": self.summary(),
            "history": [
                {"user": user, "assistant": assistant}
                for user, assistant in collect_history_pairs(state, max_turns=None)
            ],
            "active_turn": active,
            "plan": state.plan_manager.snapshot(),
            "pending_interactions": self.interactions.snapshot(),
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "context_tokens": state.context_tokens,
                "context_limit": self.runtime.llm.context_limit,
            },
        }

    def summary(self) -> dict[str, Any]:
        state = self.runtime.session_state
        return {
            "session_id": state.session_id,
            "status": "running" if not self.runtime.agent_idle.is_set() else "idle",
            "agent_status": state.status,
            "user_goal": state.user_goal,
            "model": state.model_name,
            "environment": state.environment,
            "execution_root": str(state.workspace_dir),
            "project_root": str(state.project_root or state.workspace_dir),
            "base_commit": state.base_commit,
            "branch_name": state.branch_name,
            "pending_interactions": len(self.interactions.snapshot()),
            "active": not self.closed,
            "recoverable": True,
        }

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.runtime.cancellation_event.set()
        self.interactions.close()
        self.runtime.event_queue.put(("EXIT", None))
        self.thread.join(timeout=5)
        shutdown_runtime(self.runtime)


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
        self._lock = threading.RLock()

    def project(self) -> dict[str, Any]:
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
        }

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
            try:
                runtime = build_runtime(
                    self._args(context=context, model=model, resume=resume_session_id),
                    renderer=SilentRenderer(),
                    project_context=context,
                    publisher=publisher,
                    interaction_broker=broker,
                    session_id=session_id,
                )
            except Exception:
                broker.close()
                publisher.close()
                if not resume_session_id and context.environment == "worktree":
                    self.worktrees.archive(context)
                raise
            handle = SessionHandle(runtime)
            self._handles[session_id] = handle
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
            handle = self._handles.pop(session_id, None)
        if handle is None:
            raise RuntimeManagerError("session is not active")
        summary = handle.summary()
        handle.close()
        return {**summary, "active": False, "status": "closed"}

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
        return self.worktrees.archive(context)

    def shutdown(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            handle.close()
