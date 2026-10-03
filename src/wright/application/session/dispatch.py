"""Shared session event dispatch for the CLI REPL and the fullscreen TUI.

Hosts stay separate (Repl vs Textual App). This module is only the queue
consumer: USER_INPUT, background tasks, loops, durable runs, slash commands.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from ...core.logger import get_logger
from ...infrastructure.persistence.autonomy_store import AutonomyStore
from ...infrastructure.storage.attachments import AttachmentError
from ..agent.operations import agent_completion_notice
from ..composition.runtime import WrightRuntime
from ..execution.directory import (
    DirectoryWaitCancelled,
    bind_directory_work,
    reset_directory_work,
)
from ..scheduling.contracts import SchedulingError
from ..scheduling.runner import launch_job_run
from .history_projection import (
    current_run_id,
    latest_turn_history_id,
    public_attachment,
)
from .loops import (
    SessionLoopRegistry,
    parse_loop_command,
)

logger = get_logger(__name__)

SlashHandler = Callable[[str, WrightRuntime], None]


@dataclass(frozen=True)
class SlashCommand:
    """A command's shared execution and presentation contract."""

    name: str
    description: str
    usage: str
    handler: SlashHandler | None = None


def _completion_notice(rt: WrightRuntime, identifier: str) -> dict | None:
    """Ask the command owner, then the agent control plane. No flattened task."""
    commands = rt.runtime_resources.commands
    if commands is not None:
        notice = commands.notice(identifier)
        if notice is not None:
            return notice
    return agent_completion_notice(rt.session_state.control_plane, identifier)


def _notice(rt: WrightRuntime, text: str, *, code: str = "", **params: object) -> None:
    rt.event_renderer.on_system_notice(
        text,
        code=code,
        params={key: "" if value is None else str(value) for key, value in params.items()},
    )


def _publish_execution(rt: WrightRuntime, execution: str, reason: str = "") -> None:
    rt.publisher.publish(
        "session.status_changed",
        {
            "session_id": rt.session_state.session_id,
            "lifecycle": "open",
            "execution": execution,
            "queue_reason": reason,
        },
    )


def _render_job_run_finished(store: AutonomyStore, run_id: str, rt: WrightRuntime) -> None:
    """只打一行摘要，不注入 root 上下文、不跑 Agent turn。"""
    try:
        run = store.get_run(run_id)
    except SchedulingError:
        _notice(rt, f"durable run {run_id} finished")
        return
    preview = (run.result or run.error or "").replace("\n", " ").strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    detail = f": {preview}" if preview else ""
    _notice(
        rt,
        f"schedule run {run.job_name} [{run.status}] run_id={run.id}{detail}",
    )


def _parse_external_event_command(value: str) -> tuple[str, dict]:
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("Usage: /event <name> [JSON object]")
    payload: dict = {}
    if len(parts) == 3:
        parsed = json.loads(parts[2])
        if not isinstance(parsed, dict):
            raise ValueError("event payload must be a JSON object")
        payload = parsed
    return parts[1], payload


def _handle_loop_command(value: str, registry: SessionLoopRegistry, rt: WrightRuntime) -> None:
    action, payload = parse_loop_command(value)
    if action == "list":
        records = registry.list_loops()
        if not records:
            _notice(rt, "No loop is running", code="notice.no_loops")
            return
        for record in records:
            _notice(
                rt,
                f"  {record.id}  every {record.interval_seconds:g}s  "
                f"tick={record.tick_count}  {record.name}",
            )
        return
    if action == "stop":
        record = registry.stop(str(payload))
        _notice(
            rt,
            f"Stopped loop {record.id} ({record.name})",
            code="notice.loop_stopped",
            loop_id=record.id,
            name=record.name,
        )
        return
    interval, prompt = payload
    record = registry.create(prompt=prompt, interval_seconds=interval)
    _notice(
        rt,
        f"loop {record.id} every {record.interval_seconds:g}s: {record.prompt}",
    )


def _cmd_history(text: str, rt: WrightRuntime) -> None:
    arg = text.strip()[len("/history"):].strip().lower()
    payload: dict[str, object] = {}
    if arg == "all":
        payload = {"pager": True}
    elif arg.isdigit():
        payload = {"max_turns": int(arg)}
    elif arg:
        _notice(rt, "Usage: /history [N|all]", code="notice.history_usage")
        return
    rt.publisher.publish("session.history_requested", payload)


def _cmd_help(_text: str, rt: WrightRuntime) -> None:
    lines = ["Commands:"]
    for command in SLASH_COMMANDS.values():
        lines.append(f"  {command.usage:<24} {command.description}")
    _notice(rt, "\n".join(lines))


def _cmd_status(_text: str, rt: WrightRuntime) -> None:
    session = rt.session_state
    usage = session.task_usage()
    _notice(
        rt,
        "\n".join((
            f"session  {session.session_id}  ({getattr(session, 'lifecycle', 'open')})",
            f"workspace  {session.workspace_dir}",
            (
                f"task usage  {usage.prompt_tokens:,} in  ·  "
                f"{usage.completion_tokens:,} out  ·  {usage.total_tokens:,} total"
            ),
        )),
    )


def _cmd_event(text: str, rt: WrightRuntime) -> None:
    event_name, event_payload = _parse_external_event_command(text)
    scheduler = rt.services.job_scheduler
    if scheduler is None:
        raise RuntimeError("autonomy scheduler is not configured")
    event_id = scheduler.emit_event(event_name, event_payload)
    _notice(rt, f"external event accepted (id={event_id})")


def _cmd_loop(text: str, rt: WrightRuntime) -> None:
    registry = rt.services.loop_registry
    if registry is None:
        raise RuntimeError("in-session loop runtime is not configured")
    _handle_loop_command(text, registry, rt)


SLASH_COMMANDS: dict[str, SlashCommand] = {
    "/help": SlashCommand("/help", "show available commands", "/help", _cmd_help),
    "/status": SlashCommand("/status", "show session and task usage", "/status", _cmd_status),
    "/history": SlashCommand("/history", "show prior turns", "/history [all|N]", _cmd_history),
    "/loop": SlashCommand("/loop", "create, list, or stop a loop", "/loop <interval|list|stop>", _cmd_loop),
    "/event": SlashCommand("/event", "emit an external event", "/event <name> [JSON]", _cmd_event),
    "/attach": SlashCommand("/attach", "attach one or more images", "/attach PATH..."),
    "/attachments": SlashCommand("/attachments", "list pending images", "/attachments"),
    "/detach": SlashCommand("/detach", "remove a pending image", "/detach INDEX|all"),
    "/send": SlashCommand("/send", "send pending images", "/send [prompt]"),
}


def slash_command_matches(
    text: str,
    extra_commands: tuple[SlashCommand, ...] = (),
) -> tuple[SlashCommand, ...]:
    """Return commands matching a slash-command prefix, in display order."""
    if not text.startswith("/") or any(char.isspace() for char in text):
        return ()
    prefix = text.lower()
    commands = (*SLASH_COMMANDS.values(), *extra_commands)
    return tuple(command for command in commands if command.name.startswith(prefix))


def dispatch_slash(text: str, rt: WrightRuntime) -> bool:
    """Handle a slash command. Returns True if `text` was a known command."""
    stripped = text.strip()
    if not stripped:
        return False
    head = stripped.split(maxsplit=1)[0].lower()
    command = SLASH_COMMANDS.get(head)
    if command is None:
        return False
    try:
        if command.handler is None:
            return False
        command.handler(text, rt)
    except Exception as exc:
        _notice(rt, f"{head} rejected: {exc}")
    return True


def _attachment_notice(rt: WrightRuntime) -> tuple[str, str, dict[str, str]]:
    drafts = getattr(rt, "draft_attachments", None)
    if drafts is None:
        text = "This runtime does not support attachments"
        return text, "notice.attachments_unsupported", {}
    records = drafts.summaries()
    if not records:
        text = "No images are waiting to be sent"
        return text, "notice.no_pending_images", {}
    listing = "\n".join(
        f"  [{index}] {record.filename} ({record.width}×{record.height})"
        for index, record in enumerate(records, 1)
    )
    return f"Images waiting to be sent:\n{listing}", "notice.pending_images", {"listing": listing}


def _dispatch_attachment_command(text: str, rt: WrightRuntime) -> tuple[bool, str | None]:
    """Return (handled, prompt-to-send); attachment drafts are host-local."""
    stripped = text.strip()
    head, _, tail = stripped.partition(" ")
    drafts = getattr(rt, "draft_attachments", None)
    if head == "/attach":
        if drafts is None:
            _notice(rt, "This runtime does not support attachments", code="notice.attachments_unsupported")
            return True, None
        try:
            added = drafts.attach_paths([item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item for item in shlex.split(tail, posix=os.name != "nt")])
            names = ", ".join(record.filename for record in added)
            _notice(rt, f"Attached: {names}", code="notice.attached", names=names)
        except (AttachmentError, ValueError) as exc:
            _notice(rt, str(exc))
        return True, None
    if head == "/attachments":
        text, code, params = _attachment_notice(rt)
        _notice(rt, text, code=code, **params)
        return True, None
    if head == "/detach":
        if drafts is None:
            _notice(rt, "This runtime does not support attachments", code="notice.attachments_unsupported")
            return True, None
        try:
            removed = drafts.detach(tail.strip())
            names = ", ".join(record.filename for record in removed)
            _notice(rt, f"Removed: {names}", code="notice.removed", names=names)
        except AttachmentError as exc:
            _notice(rt, str(exc))
        return True, None
    if head == "/send":
        return False, tail.strip()
    return False, None


def _publish_turn_terminal(
    rt: WrightRuntime,
    command_id: str,
    started_turn_id: str,
    error: str | None = None,
) -> None:
    """Publish the terminal event after the session record exists.

    The history id is the turn or run that this event makes visible. It is
    recorded by the publisher in the same lock as the sequence number.
    """

    session = rt.session_state
    run_id = current_run_id(session)
    identity = latest_turn_history_id(session, run_id) or run_id or started_turn_id
    payload: dict[str, object] = {"command_id": command_id, "run_id": run_id}
    if error is not None:
        payload["error"] = error
        rt.publisher.publish("turn.failed", payload, turn_id=identity)
        return
    if rt.cancellation_event.is_set():
        rt.publisher.publish("turn.cancelled", payload, turn_id=identity)
    elif session.current_run_status() == "completed":
        rt.publisher.publish("turn.completed", payload, turn_id=identity)
    else:
        payload["status"] = session.current_run_status()
        rt.publisher.publish("turn.failed", payload, turn_id=identity)


def process_session_event(
    rt: WrightRuntime,
    event_type: str,
    payload: object,
) -> bool:
    """Consume one event from the session queue.

    Returns True when the host should stop (EXIT).
    """
    if event_type == "EXIT":
        return True

    session_state = rt.session_state
    services = rt.services
    agent_idle = rt.agent_idle
    loop_registry = services.loop_registry
    job_scheduler = services.job_scheduler
    background_runtime = services.agent_background

    if event_type == "USER_INPUT":
        agent_idle.clear()
        rt.cancellation_event.clear()
        if isinstance(payload, dict):
            user_input = str(payload.get("prompt", ""))
            command_id = str(payload.get("command_id", ""))
            attachment_ids = [
                str(item) for item in payload.get("attachment_ids", [])
                if isinstance(item, str)
            ]
            references = [
                item for item in payload.get("references", [])
                if isinstance(item, dict)
            ]
        else:
            user_input = str(payload)
            command_id = ""
            attachment_ids = []
            references = []
        handled, send_prompt = _dispatch_attachment_command(user_input, rt)
        if handled:
            agent_idle.set()
            return False
        if send_prompt is not None:
            user_input = send_prompt
        if dispatch_slash(user_input, rt):
            agent_idle.set()
            return False
        if not attachment_ids:
            drafts = getattr(rt, "draft_attachments", None)
            if drafts is not None:
                attachment_ids = drafts.consume()
        if not user_input.strip() and not attachment_ids and not references:
            _notice(
                rt,
                "Type a message, or attach an image with /attach first",
                code="notice.need_input",
            )
            agent_idle.set()
            return False
        captured: list[dict] = []
        if references:
            from ..workspace.references import (
                MAX_REFERENCE_BYTES,
                capture_references,
                render_captures,
            )

            external = rt.agent.executor.permissions.reference_roots(session_state)
            captured = capture_references(
                project_root=rt.project_context.project_root,
                execution_root=rt.project_context.execution_root,
                references=references,
                reader=lambda path: rt.agent.executor.permissions.read_file(session_state, rt.agent.executor.backend, path, max_bytes=MAX_REFERENCE_BYTES + 1),
                external_roots=external,
            )
            user_input = render_captures(user_input, captured)
        turn_id = f"{session_state.session_id}:{len(session_state.message_records)}"
        coordinator = getattr(rt, "directory_coordinator", None)
        lease = None
        token = None
        if coordinator is not None:
            try:
                lease = coordinator.acquire(
                    rt.project_context.execution_root,
                    kind="turn",
                    holder_id=command_id or turn_id,
                    session_id=session_state.session_id,
                    label=f"session {session_state.session_id}",
                    cancel=rt.cancellation_event,
                    on_queued=lambda reason: _publish_execution(rt, "queued", reason),
                )
            except DirectoryWaitCancelled:
                _publish_execution(rt, "idle")
                agent_idle.set()
                return False
            token = bind_directory_work(coordinator, lease)
            _publish_execution(rt, "running")
        rt.publisher.publish(
            "turn.started",
            {
                "prompt": user_input,
                "command_id": command_id,
                "references": captured,
                "attachments": [
                    public_attachment(record)
                    for record in session_state.attachment_records(attachment_ids)
                ],
            },
            turn_id=turn_id,
        )
        try:
            rt.agent.run(
                user_input,
                cancellation_check=rt.cancellation_event.is_set,
                attachment_ids=attachment_ids,
            )
            _publish_turn_terminal(rt, command_id, turn_id)
        except Exception as exc:
            _publish_turn_terminal(rt, command_id, turn_id, error=str(exc))
            raise
        finally:
            if token is not None:
                reset_directory_work(token)
            if lease is not None:
                lease.release()
            if coordinator is not None:
                _publish_execution(rt, "idle")
            agent_idle.set()
        return False

    if event_type == "TASK_DONE":
        agent_idle.clear()
        try:
            notice = _completion_notice(rt, str(payload))
            if notice is None:
                logger.warning("ignored unknown background completion event: %s", payload)
            else:
                task = notice["task"]
                active = rt.session_state.active_run_id
                task_run = task.get("run_id") or ""
                identifier = task.get("command_id") or task.get("agent_task_id") or payload
                if task_run and task_run != active:
                    _notice(
                        rt,
                        f"background task {identifier} for run {task_run} finished: {task.get('status')}",
                    )
                else:
                    rt.agent.run_runtime_event(notice)
        finally:
            agent_idle.set()
        return False

    if event_type == "DURABLE_RUN_DUE":
        if job_scheduler is None or background_runtime is None or services.durable_store is None:
            logger.error("durable run dispatch missing scheduler/runtime: %s", payload)
            return False
        coordinator = getattr(rt, "directory_coordinator", None)
        lease = None
        if coordinator is not None:
            try:
                lease = coordinator.acquire(
                    rt.project_context.execution_root,
                    kind="automation",
                    holder_id=str(payload),
                    session_id=session_state.session_id,
                    label=f"automation run {payload}",
                    cancel=rt.cancellation_event,
                    on_queued=lambda reason: _publish_execution(rt, "queued", reason),
                )
            except DirectoryWaitCancelled:
                return False
        try:
            launch_job_run(
                run_id=str(payload),
                execution=services.durable_store,
                root_session=session_state,
                scheduler=job_scheduler,
                llm=rt.llm,
                base_tools=rt.assembled_base_tools,
                permission_settings=rt.permission_settings,
                background_runtime=background_runtime,
                lifecycle=rt.lifecycle,
                services=services,
                directory_coordinator=coordinator,
                directory_lease=lease,
            )
        except Exception:
            logger.exception("durable task dispatch failed: %s", payload)
        return False

    if event_type == "DURABLE_RUN_FINISHED":
        _render_job_run_finished(rt.autonomy_store, str(payload), rt)
        return False

    if event_type == "LOOP_DUE":
        agent_idle.clear()
        try:
            record = (
                loop_registry.begin_tick(str(payload))
                if loop_registry is not None
                else None
            )
            if record is not None and loop_registry is not None:
                rt.agent.run_runtime_event(loop_registry.runtime_event(record))
        except Exception:
            logger.exception("session loop execution failed: %s", payload)
        finally:
            if loop_registry is not None:
                loop_registry.finish_tick(str(payload))
            agent_idle.set()
        return False

    if event_type == "AUTONOMY_ERROR":
        logger.error("autonomy scheduler error: %s", payload)
        _notice(rt, f"autonomy error: {payload}")
        return False

    logger.warning("unknown session event: %s", event_type)
    return False
