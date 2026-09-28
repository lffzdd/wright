"""Wright command-line arguments.

Parsing stops at an argparse namespace. ``runtime_config_from_args`` is the
CLI adapter that turns that namespace into ``RuntimeConfig``. The application
assembler receives the config and does not parse CLI data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...application.composition.runtime import RuntimeConfig


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wright coding agent")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION_ID",
        help="从指定 session checkpoint 恢复 (留空则列出历史菜单选择)",
    )
    resume_group.add_argument(
        "-c",
        "--continue",
        dest="continue_latest",
        action="store_true",
        help="恢复最近保存的 session checkpoint",
    )
    parser.add_argument(
        "--no-session-persistence",
        action="store_true",
        help="本次运行不保存 checkpoint",
    )
    parser.add_argument(
        "--hooks-config",
        metavar="PATH",
        help="显式启用指定 lifecycle command hooks 配置（不会自动执行仓库配置）",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        metavar="DIR",
        help="要编辑的项目目录 (默认: 当前工作目录)",
    )
    parser.add_argument(
        "--ui",
        choices=("cli", "tui", "web", "headless"),
        default="tui",
        help="界面：tui（默认）、cli、本机 Web 控制台或 headless 自动任务宿主",
    )
    parser.add_argument(
        "--automation-session",
        metavar="SESSION_ID",
        help="headless 宿主要承载的 Automation 来源 session_id（不会扫描历史项目）",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="覆盖 OPENAI_MODEL；TUI 的 /model 使用同一配置",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "chat", "responses"),
        default=None,
        help="主模型协议：auto（默认）、chat 或 responses",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=0,
        metavar="PORT",
        help="Web 控制台端口（默认 0：自动选择空闲端口）",
    )
    parser.add_argument(
        "--web-capacity",
        type=int,
        default=4,
        metavar="N",
        help="Web 活跃 session 上限（默认 4）",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="启动 Web 控制台时不自动打开浏览器",
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="允许启动项目 .wright/mcp.json 中声明的进程或远程连接",
    )
    parser.add_argument(
        "--mode",
        choices=("coding", "general"),
        default="coding",
        help="运行模式：coding（默认，包含写文件与执行命令）或 general（通用助理，安全只读与分析）",
    )
    parser.add_argument(
        "--with-rag",
        action="store_true",
        help="显式挂载 RAG 外部知识库检索工具（knowledge_search）",
    )
    return parser.parse_args()


def runtime_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    """Translate an entry-point namespace once; assembly never parses CLI data."""
    hooks = getattr(args, "hooks_config", None)
    return RuntimeConfig(
        workspace=getattr(args, "workspace", None),
        resume=getattr(args, "resume", None),
        continue_latest=bool(getattr(args, "continue_latest", False)),
        no_session_persistence=bool(getattr(args, "no_session_persistence", False)),
        hooks_config=Path(hooks) if hooks else None,
        model=getattr(args, "model", None),
        transport=getattr(args, "transport", None),
        trust_project_mcp=bool(getattr(args, "trust_project_mcp", False)),
        with_rag=bool(getattr(args, "with_rag", False)),
        mode=str(getattr(args, "mode", "coding") or "coding"),
    )


