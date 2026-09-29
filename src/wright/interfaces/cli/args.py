"""Wright command-line arguments.

Parsing stops at an argparse namespace. ``runtime_config_from_args`` is the
CLI adapter that turns that namespace into ``RuntimeConfig``. The application
assembler receives the config and does not parse CLI data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...application.composition.runtime import RuntimeConfig
from ..i18n import t


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=t("cli.help.description"))
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION_ID",
        help=t("cli.help.resume"),
    )
    resume_group.add_argument(
        "-c",
        "--continue",
        dest="continue_latest",
        action="store_true",
        help=t("cli.help.continue"),
    )
    parser.add_argument(
        "--no-session-persistence",
        action="store_true",
        help=t("cli.help.no_persistence"),
    )
    parser.add_argument(
        "--hooks-config",
        metavar="PATH",
        help=t("cli.help.hooks"),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        metavar="DIR",
        help=t("cli.help.workspace"),
    )
    parser.add_argument(
        "--ui",
        choices=("cli", "tui", "web", "headless"),
        default="tui",
        help=t("cli.help.ui"),
    )
    parser.add_argument(
        "--automation-session",
        metavar="SESSION_ID",
        help=t("cli.help.automation_session"),
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help=t("cli.help.model"),
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "chat", "responses"),
        default=None,
        help=t("cli.help.transport"),
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=0,
        metavar="PORT",
        help=t("cli.help.web_port"),
    )
    parser.add_argument(
        "--web-capacity",
        type=int,
        default=4,
        metavar="N",
        help=t("cli.help.web_capacity"),
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help=t("cli.help.no_open"),
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help=t("cli.help.trust_mcp"),
    )
    parser.add_argument(
        "--environment",
        choices=("local", "worktree"),
        default=None,
        help=t("cli.help.environment"),
    )
    parser.add_argument(
        "--mode",
        choices=("coding", "general"),
        default="coding",
        help=t("cli.help.mode"),
    )
    parser.add_argument(
        "--with-rag",
        action="store_true",
        help=t("cli.help.rag"),
    )
    parser.add_argument(
        "--interface-language",
        choices=("en", "zh-CN"),
        default=None,
        metavar="LOCALE",
        help=t("cli.help.interface_language"),
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


