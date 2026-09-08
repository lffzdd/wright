"""Event-driven REPL over a constructed WrightRuntime."""

from __future__ import annotations

import queue
import threading

from prompt_toolkit import PromptSession

from .interaction import PROMPT_INTERRUPTED
from .logger import get_logger
from .renderer import ConsoleRenderer
from .runtime import WrightRuntime
from .session_host import process_session_event

logger = get_logger(__name__)


def _start_input_reader(
    renderer: ConsoleRenderer,
    event_queue: queue.Queue[tuple[str, object]],
    agent_idle: threading.Event,
) -> threading.Thread:
    """唯一读 stdin 的线程：主指令、权限/提问收集都走这里。

    主输入框只在 Agent 空闲时出现，避免和 Live 抢同一块屏幕。
    忙碌时阻塞等待空闲或 InteractionHub 请求；权限到来后由本线程收集并回传。
    """
    def read() -> None:
        prompt_session: PromptSession[str] = PromptSession()
        hub = getattr(renderer, "_hub", None)
        if hub is not None:
            hub.bind_collector(interrupt=renderer.interrupt_main_prompt)
        while True:
            if hub is not None:
                request = hub.poll()
                if request is not None:
                    renderer.fulfill_interaction(request)
                    continue
                if not agent_idle.is_set():
                    hub.wait_for_idle_or_request(agent_idle)
                    continue
            elif not agent_idle.is_set():
                agent_idle.wait()
                continue
            value = renderer.prompt_main_input(prompt_session)
            if value is PROMPT_INTERRUPTED:
                continue
            if value is None or value in {"/exit", "/quit"}:
                event_queue.put(("EXIT", None))
                return
            if value:
                # 先清 idle，避免主线程还没开始跑就又画出第二个「你的指令」。
                agent_idle.clear()
                event_queue.put(("USER_INPUT", value))

    thread = threading.Thread(target=read, name="wright-input", daemon=True)
    thread.start()
    return thread


class Repl:
    def __init__(self, rt: WrightRuntime) -> None:
        self.rt = rt

    def run(self) -> None:
        rt = self.rt
        session_state = rt.session_state
        services = rt.services
        agent_idle = rt.agent_idle
        event_queue = rt.event_queue
        if services.autonomy_scheduler is None or services.agent_background is None:
            raise RuntimeError("REPL requires autonomy scheduler and background runtime")

        if not isinstance(rt.renderer, ConsoleRenderer):
            raise TypeError("CLI host requires ConsoleRenderer")

        # 只有这个循环会改 root SessionState。durable run 在这里构造独立会话后
        # 丢给后台，完成时只渲染摘要，不再走 run_runtime_event。
        #
        # agent_idle：loop 调度与主输入框的同步信号。
        #   主线程在 agent.run() 前 clear()，结束后 set()；
        #   输入线程空闲才画「你的指令」，忙碌只响应 InteractionHub。
        #   loop 只在 set()（空闲）时投递 LOOP_DUE。
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
            if process_session_event(rt, event_type, payload):
                break
