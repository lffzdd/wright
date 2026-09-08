"""Shared session event dispatch for the CLI REPL and the fullscreen TUI.

Hosts stay separate (Repl vs Textual App). This module is only the queue
consumer: USER_INPUT, background tasks, loops, durable runs, slash commands.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .autonomy import AutonomyStore, AutonomyStoreError
from .autonomy.runner import launch_durable_run
from .logger import get_logger
from .looping import SessionLoopRegistry, parse_loop_command
from .runtime import WrightRuntime
from .tasks import RuntimeTask, TaskNotFoundError, TaskService

logger = get_logger(__name__)

SlashHandler = Callable[[str, WrightRuntime], None]


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
    rt.renderer.on_system_notice(text)


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


SLASH_COMMANDS: dict[str, SlashHandler] = {
    "/history": _cmd_history,
    "/event": _cmd_event,
    "/loop": _cmd_loop,
}


def dispatch_slash(text: str, rt: WrightRuntime) -> bool:
    """Handle a slash command. Returns True if `text` was a known command."""
    stripped = text.strip()
    if not stripped:
        return False
    head = stripped.split(maxsplit=1)[0].lower()
    handler = SLASH_COMMANDS.get(head)
    if handler is None:
        return False
    try:
        handler(text, rt)
    except Exception as exc:
        _notice(rt, f"{head} rejected: {exc}")
    return True


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
        user_input = str(payload)
        if dispatch_slash(user_input, rt):
            agent_idle.set()
            return False
        try:
            rt.agent.run(user_input)
        finally:
            agent_idle.set()
        return False

    if event_type == "TASK_DONE":
        agent_idle.clear()
        try:
            try:
                task = TaskService.for_session(session_state, services).get(str(payload))
            except TaskNotFoundError:
                logger.warning("忽略未知后台任务完成事件: %s", payload)
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
