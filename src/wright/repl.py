"""Event-driven REPL over a constructed WrightRuntime."""

from __future__ import annotations

import json
import queue
import threading
from typing import Callable

from prompt_toolkit import PromptSession

from .autonomy import AutonomyStore, AutonomyStoreError
from .autonomy.runner import launch_durable_run
from .logger import get_logger
from .looping import SessionLoopRegistry, parse_loop_command
from .renderer import ConsoleRenderer
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


def _render_durable_run_finished(store: AutonomyStore, run_id: str) -> None:
    """只打一行摘要，不注入 root 上下文、不跑 Agent turn。"""
    try:
        run = store.get_run(run_id)
    except AutonomyStoreError:
        print(f"⏰ durable run {run_id} finished")
        return
    preview = (run.result or run.error or "").replace("\n", " ").strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    detail = f": {preview}" if preview else ""
    print(
        f"⏰ durable run {run.automation_name} [{run.status}] "
        f"({run.id}){detail}"
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


def _handle_loop_command(value: str, registry: SessionLoopRegistry) -> None:
    action, payload = parse_loop_command(value)
    if action == "list":
        records = registry.list_loops()
        if not records:
            print("没有运行中的 loop")
            return
        for record in records:
            print(
                f"  {record.id}  every {record.interval_seconds:g}s  "
                f"tick={record.tick_count}  {record.name}"
            )
        return
    if action == "stop":
        record = registry.stop(str(payload))
        print(f"已停止 loop {record.id} ({record.name})")
        return
    interval, prompt = payload
    record = registry.create(prompt=prompt, interval_seconds=interval)
    print(
        f"🔁 loop {record.id} every {record.interval_seconds:g}s: {record.prompt}"
    )


def _start_input_reader(
    renderer: ConsoleRenderer,
    event_queue: "queue.Queue[tuple[str, object]]",
    agent_idle: threading.Event,
) -> threading.Thread:
    """Keep terminal input blocking away from the session event consumer.

    agent_idle 由主线程维护：
      - agent.run() 结束后 set()（空闲）
      - agent.run() 开始前 clear()（忙碌）

    输入线程在【渲染提示符之前】先 wait()，确保 agent 空闲后才让用户看到输
    入框；用户提交后立刻 clear() 再 put()，避免主线程来不及 clear 就被下一
    次 wait() 穿透的竞态。
    """
    def read() -> None:
        prompt_session: PromptSession[str] = PromptSession()
        while True:
            # 先等 agent 空闲，再渲染输入提示符——保证提示符不会出现在回答中间
            agent_idle.wait()
            value = renderer.prompt_main_input(prompt_session)
            if value is None or value in {"/exit", "/quit"}:
                event_queue.put(("EXIT", None))
                return
            if value:
                # clear() 必须在 put() 之前：主线程 get() 后才 clear，
                # 若放在 put() 后则 wait() 可能在主线程 clear() 前就穿透。
                agent_idle.clear()
                event_queue.put(("USER_INPUT", value))

    thread = threading.Thread(target=read, name="wright-input", daemon=True)
    thread.start()
    return thread


def _cmd_history(text: str, rt: WrightRuntime) -> None:
    arg = text.strip()[len("/history"):].strip().lower()
    if arg == "all":
        rt.renderer.render_session_history(rt.session_state, pager=True)
    elif arg.isdigit():
        rt.renderer.render_session_history(
            rt.session_state, max_turns=int(arg)
        )
    else:
        rt.renderer.render_session_history(rt.session_state)


def _cmd_event(text: str, rt: WrightRuntime) -> None:
    event_name, event_payload = _parse_external_event_command(text)
    scheduler = rt.services.autonomy_scheduler
    if scheduler is None:
        raise RuntimeError("autonomy scheduler is not configured")
    event_id = scheduler.emit_event(event_name, event_payload)
    print(f"📨 external event accepted (id={event_id})")


def _cmd_loop(text: str, rt: WrightRuntime) -> None:
    registry = rt.services.loop_registry
    if registry is None:
        raise RuntimeError("in-session loop runtime is not configured")
    _handle_loop_command(text, registry)


SLASH_COMMANDS: dict[str, SlashHandler] = {
    "/history": _cmd_history,
    "/event": _cmd_event,
    "/loop": _cmd_loop,
}


class Repl:
    def __init__(self, rt: WrightRuntime) -> None:
        self.rt = rt

    def _dispatch_slash(self, text: str) -> bool:
        head = text.strip().split(maxsplit=1)[0].lower()
        handler = SLASH_COMMANDS.get(head)
        if handler is None:
            return False
        try:
            handler(text, self.rt)
        except Exception as exc:
            print(f"{head} rejected: {exc}")
        finally:
            self.rt.agent_idle.set()
        return True

    def run(self) -> None:
        rt = self.rt
        session_state = rt.session_state
        services = rt.services
        agent_idle = rt.agent_idle
        event_queue = rt.event_queue
        loop_registry = services.loop_registry
        autonomy_scheduler = services.autonomy_scheduler
        background_runtime = services.agent_background
        if autonomy_scheduler is None or background_runtime is None:
            raise RuntimeError("REPL requires autonomy scheduler and background runtime")

        # 只有这个循环会改 root SessionState。durable run 在这里构造独立会话后
        # 丢给后台，完成时只渲染摘要，不再走 run_runtime_event。
        #
        # agent_idle：输入线程、loop 调度线程与主线程之间的同步信号。
        #   主线程在 agent.run() 前 clear()，结束后 set()；
        #   输入线程 put 完事件后 wait()，确保下一个提示符在回答渲染完毕后才出现。
        #   loop 只在 set()（空闲）时投递 LOOP_DUE，忙时错过的周期合并成一次。
        if rt.resumed:
            print(
                f"已恢复 session {session_state.session_id} "
                f"(status={session_state.status})"
            )
            rt.renderer.render_session_history(session_state)
            if session_state.status == "running":
                rt.agent.continue_run()
        _start_input_reader(rt.renderer, event_queue, agent_idle)
        while True:
            event_type, payload = event_queue.get()
            if event_type == "EXIT":
                break
            if event_type == "USER_INPUT":
                # agent_idle 已由输入线程在 put() 前 clear()，无需重复 clear
                user_input = str(payload)
                if self._dispatch_slash(user_input):
                    continue
                try:
                    rt.agent.run(user_input)
                finally:
                    agent_idle.set()

            elif event_type == "TASK_DONE":
                # Agent/Shell 都只发送 task_id；主线程从唯一真实 owner 投影出
                # 同一种 RuntimeTask 后再唤醒 Agent。
                agent_idle.clear()
                try:
                    try:
                        task = TaskService.for_session(
                            session_state, services
                        ).get(str(payload))
                    except TaskNotFoundError:
                        logger.warning("忽略未知后台任务完成事件: %s", payload)
                    else:
                        rt.agent.run_runtime_event(_task_notification_event(task))
                finally:
                    agent_idle.set()

            elif event_type == "DURABLE_RUN_DUE":
                # 只派发，不阻塞事件循环。构造 run 仍只允许本线程。
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

            elif event_type == "DURABLE_RUN_FINISHED":
                _render_durable_run_finished(rt.autonomy_store, str(payload))

            elif event_type == "LOOP_DUE":
                agent_idle.clear()
                try:
                    record = (
                        loop_registry.begin_tick(str(payload))
                        if loop_registry is not None
                        else None
                    )
                    if record is not None and loop_registry is not None:
                        rt.agent.run_runtime_event(
                            loop_registry.runtime_event(record)
                        )
                except Exception:
                    logger.exception("session loop execution failed: %s", payload)
                finally:
                    if loop_registry is not None:
                        loop_registry.finish_tick(str(payload))
                    agent_idle.set()

            elif event_type == "AUTONOMY_ERROR":
                logger.error("autonomy scheduler error: %s", payload)
