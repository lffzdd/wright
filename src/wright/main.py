"""
Wright 主入口：终端里的 coding agent。

当前回合协议仍是多工具 ReAct JSON 信封；主循环一次可发起多个工具调用。
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
