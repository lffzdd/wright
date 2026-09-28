"""Explicit local-only host for persisted durable automations.

Headless execution has no terminal collector. Permission and ask_user fail
closed instead of waiting on a prompt nobody can answer.
"""

from __future__ import annotations

import threading

from ..application.composition.runtime import (
    RuntimeConfig,
    assemble_runtime,
    shutdown_runtime,
)
from ..application.session.interaction import DeniedPrompter


def run_headless_host(
    config: RuntimeConfig,
    *,
    source_session_id: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Run one opened project's persisted automations until explicitly stopped."""
    runtime = assemble_runtime(
        config,
        prompter=DeniedPrompter(),
        start_automation=False,
        automation_session_id=source_session_id,
    )
    host = runtime.application_host
    if host is None:
        raise RuntimeError("headless runtime has no application host")
    stop = stop_event or threading.Event()
    try:
        host.start()
        print(
            f"Wright headless host running for {runtime.project_context.execution_root}; "
            "press Ctrl-C to stop"
        )
        stop.wait()
    finally:
        shutdown_runtime(runtime)
