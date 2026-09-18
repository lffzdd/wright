"""Shared session event dispatch for the CLI REPL and the fullscreen TUI.

Hosts stay separate (Repl vs Textual App). This module is only the queue
consumer: USER_INPUT, background tasks, loops, durable runs, slash commands.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from .attachments import AttachmentError
from .autonomy import AutonomyStore, AutonomyStoreError
from .autonomy.runner import launch_durable_run
from .logger import get_logger
from .looping import SessionLoopRegistry, parse_loop_command
from .runtime import WrightRuntime
from .tasks import RuntimeTask, TaskNotFoundError, TaskService

logger = get_logger(__name__)

SlashHandler = Callable[[str, WrightRuntime], None]


@dataclass(frozen=True)
class SlashCommand:
    """A command's shared execution and presentation contract."""

    name: str
    description: str
    usage: str
    handler: SlashHandler | None = None


def _task_notification_event(task: RuntimeTask) -> dict:
    """Adapt a RuntimeTask into a runtime-event envelope for agent.run_runtime_event()."""
    return {
        "type": "task_notification",
        "task": {
            "id": task.id[:100],
            "kind": task.kind,
            "root_turn_id": task.root_turn_id[:180],
            "status": task.status,
            "description": task.description[:500],
            "result": task.result[:2_000],
            "output": task.output[-2_000:],
            "error": task.error[:1_000],
            "returncode": task.returncode,
            "cancel_requested": task.cancel_requested,
            "cancel_reason": task.cancel_reason[:500],
        },
    }


def _notice(rt: WrightRuntime, text: str) -> None:
    getattr(rt, "event_renderer", rt.renderer).on_system_notice(text)


def _render_durable_run_finished(store: AutonomyStore, run_id: str, rt: WrightRuntime) -> None:
    """只打一行摘要，不注入 root 上下文、不跑 Agent turn。"""
    try:
        run = store.get_run(run_id)
    except AutonomyStoreError:
        _notice(rt, f"durable run {run_id} finished")
        return
    preview = (run.result or run.error or "").replace("\n", " ").strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    detail = f": {preview}" if preview else ""
    _notice(
        rt,
        f"durable run {run.automation_name} [{run.status}] ({run.id}){detail}",
    )


def _parse_external_event_command(value: str) -> tuple[str, dict]:
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("用法: /event <name> [JSON object]")
    payload: dict = {}
    if len(parts) == 3:
        parsed = json.loads(parts[2])
        if not isinstance(parsed, dict):
            raise ValueError("event payload 必须是 JSON object")
        payload = parsed
    return parts[1], payload


def _handle_loop_command(value: str, registry: SessionLoopRegistry, rt: WrightRuntime) -> None:
    action, payload = parse_loop_command(value)
    if action == "list":
        records = registry.list_loops()
        if not records:
            _notice(rt, "没有运行中的 loop")
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
        _notice(rt, f"已停止 loop {record.id} ({record.name})")
        return
    interval, prompt = payload
    record = registry.create(prompt=prompt, interval_seconds=interval)
    _notice(
        rt,
        f"loop {record.id} every {record.interval_seconds:g}s: {record.prompt}",
    )


def _cmd_history(text: str, rt: WrightRuntime) -> None:
    arg = text.strip()[len("/history"):].strip().lower()
    render = getattr(rt.renderer, "render_session_history", None)
    if not callable(render):
        _notice(rt, "对话记录就在上方，滚动即可")
        return
    if arg == "all":
        render(rt.session_state, pager=True)
    elif arg.isdigit():
        render(rt.session_state, max_turns=int(arg))
    else:
        render(rt.session_state)


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
    scheduler = rt.services.autonomy_scheduler
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


def _attachment_notice(rt: WrightRuntime) -> str:
    drafts = getattr(rt, "draft_attachments", None)
    if drafts is None:
        return "当前运行时不支持附件"
    records = drafts.summaries()
    if not records:
        return "没有待发送图片"
    return "待发送图片:\n" + "\n".join(
        f"  [{index}] {record.filename} ({record.width}×{record.height})"
        for index, record in enumerate(records, 1)
    )


def _dispatch_attachment_command(text: str, rt: WrightRuntime) -> tuple[bool, str | None]:
    """Return (handled, prompt-to-send); attachment drafts are host-local."""
    stripped = text.strip()
    head, _, tail = stripped.partition(" ")
    drafts = getattr(rt, "draft_attachments", None)
    if head == "/attach":
        if drafts is None:
            _notice(rt, "当前运行时不支持附件")
            return True, None
        try:
            added = drafts.attach_paths(shlex.split(tail))
            _notice(rt, "已附加: " + ", ".join(record.filename for record in added))
        except (AttachmentError, ValueError) as exc:
            _notice(rt, str(exc))
        return True, None
    if head == "/attachments":
        _notice(rt, _attachment_notice(rt))
        return True, None
    if head == "/detach":
        if drafts is None:
            _notice(rt, "当前运行时不支持附件")
            return True, None
        try:
            removed = drafts.detach(tail.strip())
            _notice(rt, "已移除: " + ", ".join(record.filename for record in removed))
        except AttachmentError as exc:
            _notice(rt, str(exc))
        return True, None
    if head == "/send":
        return False, tail.strip()
    return False, None


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
    autonomy_scheduler = services.autonomy_scheduler
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
        else:
            user_input = str(payload)
            command_id = ""
            attachment_ids = []
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
        if not user_input.strip() and not attachment_ids:
            _notice(rt, "请输入文字或先用 /attach 添加图片")
            agent_idle.set()
            return False
        turn_id = f"{session_state.session_id}:{len(session_state.message_records)}"
        rt.publisher.publish(
            "turn.started",
            {
                "prompt": user_input,
                "command_id": command_id,
                "attachments": [
                    record.to_dict()
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
            if rt.cancellation_event.is_set():
                rt.publisher.publish(
                    "turn.cancelled", {"command_id": command_id}, turn_id=turn_id
                )
            elif session_state.current_run_status() == "completed":
                rt.publisher.publish(
                    "turn.completed", {"command_id": command_id}, turn_id=turn_id
                )
            else:
                rt.publisher.publish(
                    "turn.failed",
                    {
                        "command_id": command_id,
                        "status": session_state.current_run_status(),
                    },
                    turn_id=turn_id,
                )
        except Exception as exc:
            rt.publisher.publish(
                "turn.failed",
                {"command_id": command_id, "error": str(exc)},
                turn_id=turn_id,
            )
            raise
        finally:
            agent_idle.set()
        return False

    if event_type == "TASK_DONE":
        agent_idle.clear()
        try:
            try:
                task = TaskService.for_session(
                    session_state, services, rt.runtime_resources
                ).get(str(payload))
            except TaskNotFoundError:
                logger.warning("忽略未知后台任务完成事件: %s", payload)
            else:
                active = rt.session_state.active_run_id
                task_run = task.run_id
                if task_run and task_run != active:
                    # A completion from A must not become evidence for B.  The
                    # event remains observable; a later continuation-run phase
                    # can elect to reason over it with A's budget/lineage.
                    _notice(rt, f"background task {task.id} for run {task_run} finished: {task.status}")
                else:
                    rt.agent.run_runtime_event(_task_notification_event(task))
        finally:
            agent_idle.set()
        return False

    if event_type == "DURABLE_RUN_DUE":
        if autonomy_scheduler is None or background_runtime is None:
            logger.error("durable run dispatch missing scheduler/runtime: %s", payload)
            return False
        try:
            launch_durable_run(
                run_id=str(payload),
                root_session=session_state,
                scheduler=autonomy_scheduler,
                llm=rt.llm,
                base_tools=rt.assembled_base_tools,
                permission_settings=rt.permission_settings,
                background_runtime=background_runtime,
                lifecycle=rt.lifecycle,
                services=services,
            )
        except Exception:
            logger.exception("durable task dispatch failed: %s", payload)
        return False

    if event_type == "DURABLE_RUN_FINISHED":
        _render_durable_run_finished(rt.autonomy_store, str(payload), rt)
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
