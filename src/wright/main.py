"""
Wright 主入口：终端里的 coding agent。

通过原生工具调用驱动主循环，一轮可执行多个工具并回传各自结果。
"""

import os
import sys

from .repl import Repl
from .runtime import build_runtime, parse_cli_args, shutdown_runtime


def main() -> None:
    args = parse_cli_args()
    # A fullscreen app only makes sense on an interactive terminal. Keep the
    # default pleasant for people while preserving text behavior for scripts.
    if args.ui == "tui" and sys.stdin.isatty() and sys.stdout.isatty():
        # Textual's Kitty keyboard protocol may report macOS IME candidate keys
        # as ordinary printable keys. Prefer reliable system input methods over
        # that optional enhanced-key protocol in every macOS terminal.
        if sys.platform == "darwin":
            os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
        from .tui import run_tui

        run_tui(args)
        return
    rt = build_runtime(args)
    try:
        Repl(rt).run()
        if rt.agent.checkpoint_store:
            print(f"💾 会话已保存 (session_id: {rt.session_state.session_id})")
    finally:
        shutdown_runtime(rt)


if __name__ == "__main__":
    main()
