"""Wright entry point: a coding agent in the terminal."""

import os
import sys

from pathlib import Path

from .application.composition.runtime import load_env
from .application.session.directory import SessionDirectory, SessionDirectoryError
from .interfaces.cli.args import parse_cli_args, runtime_config_from_args
from .interfaces.i18n import activate_saved_locale, set_locale, t
from .interfaces.cli.console_renderer import ConsoleRenderer
from .interfaces.cli.prompter import ConsolePrompter
from .interfaces.cli.repl import Repl
from .interfaces.cli.resume_select import choose_resume_session
from .interfaces.interaction import InteractionHub
from .interfaces.rendering.attach import attach_renderer
from .interfaces.tui.terminal import configure_terminal


def main() -> None:
    activate_saved_locale()
    args = parse_cli_args()
    if getattr(args, "interface_language", None):
        set_locale(args.interface_language, persist=True)
    load_env()
    if args.ui == "web":
        try:
            from .interfaces.web.server import run_web
        except ImportError as exc:
            raise SystemExit(t("cli.web_missing")) from exc
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
    config = runtime_config_from_args(args)
    workspace = Path(config.workspace or Path.cwd()).expanduser().resolve()
    directory = SessionDirectory(workspace, base_args=args)
    try:
        opened = directory.open(
            environment=getattr(args, "environment", None),
            model=config.model,
            resume=getattr(args, "resume", None),
            continue_latest=config.continue_latest,
            interaction_broker=hub,
            resume_chooser=choose_resume_session,
        )
    except SessionDirectoryError as exc:
        raise SystemExit(str(exc)) from exc
    listener_id = attach_renderer(
        opened.publisher, renderer, session=opened.runtime.session_state,
    )
    repl = Repl(
        opened.runtime,
        prompter=prompter,
        renderer=renderer,
        service=opened.service,
        directory=directory,
        resume_chooser=choose_resume_session,
        listener_id=listener_id,
    )
    try:
        repl.run()
    finally:
        directory.shutdown()
    print(t("cli.session_saved", session_id=opened.session_id))


if __name__ == "__main__":
    main()
