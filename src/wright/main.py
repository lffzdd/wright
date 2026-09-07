"""
Wright 主入口：终端里的 coding agent。

通过原生工具调用驱动主循环，一轮可执行多个工具并回传各自结果。
"""

from .repl import Repl
from .runtime import build_runtime, parse_cli_args, shutdown_runtime


def main() -> None:
    args = parse_cli_args()
    rt = build_runtime(args)
    try:
        Repl(rt).run()
        if rt.agent.checkpoint_store:
            print(f"💾 会话已保存 (session_id: {rt.session_state.session_id})")
    finally:
        shutdown_runtime(rt)


if __name__ == "__main__":
    main()
