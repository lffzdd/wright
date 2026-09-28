"""CLI entry that adds a console renderer, prompter, and the resume menu.

Shared assembly does not import this module.
"""

from __future__ import annotations

import argparse
from typing import Any

from ...application.composition.runtime import WrightRuntime, assemble_runtime
from ..interaction import InteractionHub
from ..rendering.attach import attach_renderer
from .args import runtime_config_from_args
from .console_renderer import ConsoleRenderer
from .prompter import ConsolePrompter
from .resume_select import choose_resume_session


def build_runtime(
    args: argparse.Namespace,
    **kwargs: Any,
) -> WrightRuntime:
    """Supply a console renderer and the resume menu, then assemble."""
    renderer = kwargs.pop("renderer", None)
    if renderer is None:
        renderer = ConsoleRenderer()
    hub = kwargs.pop("interaction_broker", None)
    if hub is None:
        hub = InteractionHub()
    prompter = kwargs.pop("prompter", None)
    if prompter is None:
        prompter = ConsolePrompter(renderer)
    kwargs.setdefault("resume_chooser", choose_resume_session)
    runtime = assemble_runtime(
        runtime_config_from_args(args),
        interaction_broker=hub,
        prompter=prompter,
        **kwargs,
    )
    attach_renderer(runtime.publisher, renderer, session=runtime.session_state)
    return runtime
