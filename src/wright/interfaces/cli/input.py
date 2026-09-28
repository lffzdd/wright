"""Single terminal input thread for the CLI host.

The agent thread waits on ``InteractionHub``. This thread is the only one
that reads stdin: main instructions, permission prompts, and ask_user.
It pauses the console display through public renderer methods before a
prompt and resumes it afterwards.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.formatted_text import HTML

from ..i18n import t
from ..interaction import PROMPT_INTERRUPTED, InteractionHub, InteractionRequest
from .console_renderer import ConsoleRenderer
from .prompter import ConsolePrompter

MainInput = Callable[[Any, bool], str | None]


class CliInputController:
    """Coordinate main input, interaction collection, and prompt interruption."""

    def __init__(
        self,
        *,
        service: Any,
        agent_idle: threading.Event,
        renderer: ConsoleRenderer | None = None,
        prompter: ConsolePrompter | None = None,
        hub: InteractionHub | None = None,
        read_main: MainInput | None = None,
    ) -> None:
        self._service = service
        self._agent_idle = agent_idle
        self._renderer = renderer
        self._prompter = prompter
        self._hub = hub
        self._read_main = read_main
        self._prompt_session: Any = None

    def interrupt_main_prompt(self) -> None:
        session = self._prompt_session
        app = getattr(session, "app", None)
        if app is None or not getattr(app, "is_running", False):
            return
        try:
            app.exit(result=PROMPT_INTERRUPTED)
        except Exception:  # ptk 的 exit() 在 race 时抛 Exception
            pass

    def fulfill(self, request: InteractionRequest) -> None:
        prompter = self._prompter
        try:
            if prompter is None:
                result: Any = None if request.kind == "ask_user" else "deny"
            elif request.kind == "permission":
                result = prompter.collect_permission(request.payload)
            elif request.kind == "ask_user":
                result = prompter.collect_user(request.payload)
            else:
                result = "deny"
        except Exception:  # 必须结束这次请求，否则 Agent 线程会挂
            result = None if request.kind == "ask_user" else "deny"
        if self._hub is None:
            return
        self._hub.resolve(request.request_id, result)

    def read_main_input(self, *, queueing: bool = False) -> str | None:
        if self._read_main is not None:
            return self._read_main(self._prompt_session, queueing)
        renderer = self._renderer
        if renderer is None:
            return None
        if not queueing:
            renderer.settle_for_prompt()
            label = t("cli.prompt_idle")
            prompt_text = HTML(
                f"<b><ansicyan>╭─ {label} </ansicyan></b>\n"
                "<b><ansicyan>╰─❯ </ansicyan></b>"
            )
        else:
            prompt_text = HTML(f"<b><ansibrightblack>{t('cli.prompt_queued')} ❯ </ansibrightblack></b>")

        def _abort_if_interaction_pending() -> None:
            hub = self._hub
            if hub is None or not hub.has_pending():
                return
            try:
                from prompt_toolkit.application import get_app
                get_app().exit(result=PROMPT_INTERRUPTED)
            except Exception:  # ptk 无 running app 时抛 Exception
                pass

        session = self._prompt_session
        try:
            if session is not None:
                val = session.prompt(prompt_text, pre_run=_abort_if_interaction_pending)
            else:
                val = prompt(prompt_text, pre_run=_abort_if_interaction_pending)
        except (EOFError, KeyboardInterrupt):
            renderer.show_blank()
            return None

        if val is PROMPT_INTERRUPTED:
            return PROMPT_INTERRUPTED  # type: ignore[return-value]
        if not isinstance(val, str):
            return None
        val = val.strip()
        if val and val not in {"/exit", "/quit"}:
            renderer.show_submitted_input(val, queued=queueing)
        return val

    def start(self) -> threading.Thread:
        """唯一读 stdin 的线程：主指令、权限/提问收集都走这里。"""

        def read() -> None:
            self._prompt_session = PromptSession()
            hub = self._hub
            if hub is not None:
                hub.bind_collector(interrupt=self.interrupt_main_prompt)
            while True:
                if hub is not None:
                    request = hub.poll()
                    if request is not None:
                        self.fulfill(request)
                        continue
                    if not self._agent_idle.is_set():
                        hub.wait_for_idle_or_request(self._agent_idle)
                        continue
                elif not self._agent_idle.is_set():
                    self._agent_idle.wait()
                    continue
                value = self.read_main_input()
                if value is PROMPT_INTERRUPTED:
                    continue
                if value is None or value in {"/exit", "/quit"}:
                    if hasattr(self._service, "close"):
                        self._service.close(wait_timeout=0)
                    else:
                        self._service.put(("EXIT", None))
                    return
                if value:
                    # 先清 idle，避免主线程还没开始跑就又画出第二个「你的指令」。
                    self._agent_idle.clear()
                    if hasattr(self._service, "submit"):
                        self._service.submit(value)
                    else:
                        self._service.put(("USER_INPUT", value))

        thread = threading.Thread(target=read, name="wright-input", daemon=True)
        thread.start()
        return thread


def start_cli_input(
    renderer: ConsoleRenderer,
    service: Any,
    agent_idle: threading.Event,
    *,
    prompter: ConsolePrompter,
    hub: InteractionHub | None,
) -> threading.Thread:
    return CliInputController(
        renderer=renderer,
        prompter=prompter,
        hub=hub,
        service=service,
        agent_idle=agent_idle,
    ).start()
