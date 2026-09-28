"""Event-driven REPL over a constructed WrightRuntime."""

from __future__ import annotations

from ...application.composition.runtime import WrightRuntime
from ..i18n import t
from ...application.session.service import SessionService
from .console_renderer import ConsoleRenderer
from .input import CliInputController
from .prompter import ConsolePrompter


class Repl:
    def __init__(
        self, rt: WrightRuntime, *, prompter: ConsolePrompter, renderer: ConsoleRenderer,
    ) -> None:
        self.rt = rt
        self.prompter = prompter
        self.renderer = renderer
        self.service = SessionService(rt)

    def run(self) -> None:
        rt = self.rt
        session_state = rt.session_state
        services = rt.services
        agent_idle = rt.agent_idle
        if services.autonomy_scheduler is None or services.agent_background is None:
            raise RuntimeError("REPL requires autonomy scheduler and background runtime")

        if not isinstance(self.renderer, ConsoleRenderer):
            raise TypeError("CLI host requires ConsoleRenderer")

        # 只有这个循环会改 root Session。durable run 在这里构造独立会话后
        # 丢给后台，完成时只渲染摘要，不再走 run_runtime_event。
        #
        # agent_idle：loop 调度与主输入框的同步信号。
        #   主线程在 agent.run() 前 clear()，结束后 set()；
        #   输入线程空闲才画「你的指令」，忙碌只响应 InteractionHub。
        #   loop 只在 set()（空闲）时投递 LOOP_DUE。
        if rt.resumed:
            print(t(
                "cli.resumed",
                session_id=session_state.session_id,
                status=session_state.current_run_status(),
            ))
            self.renderer.render_session_history(session_state)
        self.service.start()
        CliInputController(
            renderer=self.renderer,
            prompter=self.prompter,
            hub=rt.interaction_broker,
            service=self.service,
            agent_idle=agent_idle,
        ).start()
        try:
            self.service.runner.join()
        finally:
            self.service.close(wait_timeout=0)
