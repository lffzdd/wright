"""
Wright 主入口：终端里的 coding agent。

通过原生工具调用驱动主循环，一轮可执行多个工具并回传各自结果。
"""

import os
import sys

from .repl import Repl
from .runtime import (
    assemble_runtime,
    load_env,
    parse_cli_args,
    runtime_config_from_args,
)
from .terminal import configure_terminal


def main() -> None:
    args = parse_cli_args()
    load_env()
    if args.ui == "web":
        try:
            from .web.server import run_web
        except ImportError as exc:
            raise SystemExit(
                "Web UI 依赖未安装；请运行 `uv sync --extra web` "
                "或 `pip install 'wright[web]'`。"
            ) from exc
        run_web(args)
        return
    if args.ui == "headless":
        from .headless import run_headless_host

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
        from .tui import run_tui

        run_tui(args)
        return
    rt = assemble_runtime(runtime_config_from_args(args))
    repl = Repl(rt)
    try:
        repl.run()
    finally:
        repl.service.close(wait_timeout=5)
    if rt.agent.checkpoint_store:
        print(f"💾 会话已保存 (session_id: {rt.session_state.session_id})")


if __name__ == "__main__":
    main()
