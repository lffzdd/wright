"""
Wright 主入口：终端里的 coding agent。

通过原生工具调用驱动主循环，一轮可执行多个工具并回传各自结果。
"""

import os
import sys

from .application.composition.runtime import assemble_runtime, load_env
from .interfaces.cli.args import parse_cli_args, runtime_config_from_args
from .interfaces.cli.console_renderer import ConsoleRenderer
from .interfaces.cli.prompter import ConsolePrompter
from .interfaces.cli.repl import Repl
from .interfaces.cli.resume_select import choose_resume_session
from .interfaces.interaction import InteractionHub
from .interfaces.rendering.attach import attach_renderer
from .interfaces.tui.terminal import configure_terminal


def main() -> None:
    args = parse_cli_args()
    load_env()
    if args.ui == "web":
        try:
            from .interfaces.web.server import run_web
        except ImportError as exc:
            raise SystemExit(
                "Web UI 依赖未安装；请运行 `uv sync --extra web` "
                "或 `pip install 'wright[web]'`。"
            ) from exc
        run_web(args)
        return
    if args.ui == "headless":
        from .interfaces.headless import run_headless_host

        try:
            run_headless_host(
                runtime_config_from_args(args),
                source_session_id=args.automation_session,
            )
        except KeyboardInterrupt:
            return
        return
    # A fullscreen app only makes sense on an interactive terminal. Keep the
    # default pleasant for people while preserving text behavior for scripts.
    if args.ui == "tui" and sys.stdin.isatty() and sys.stdout.isatty():
        configure_terminal(os.environ, sys.platform)
        from .interfaces.tui import run_tui

        run_tui(args)
        return
    renderer = ConsoleRenderer()
    hub = InteractionHub()
    prompter = ConsolePrompter(renderer)
    rt = assemble_runtime(
        runtime_config_from_args(args),
        interaction_broker=hub,
        prompter=prompter,
        resume_chooser=choose_resume_session,
    )
    attach_renderer(rt.publisher, renderer, session=rt.session_state)
    repl = Repl(rt, prompter=prompter, renderer=renderer)
    try:
        repl.run()
    finally:
        repl.service.close(wait_timeout=5)
    if rt.agent.checkpoint_store:
        print(f"💾 会话已保存 (session_id: {rt.session_state.session_id})")


if __name__ == "__main__":
    main()
