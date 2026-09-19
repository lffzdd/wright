"""Explicit local-only host for persisted durable automations."""

from __future__ import annotations

import threading

from .runtime import RuntimeConfig, assemble_runtime, shutdown_runtime


def run_headless_host(
    config: RuntimeConfig,
    *,
    source_session_id: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Run one opened project's persisted automations until explicitly stopped."""
    runtime = assemble_runtime(
        config,
        start_automation=False,
        automation_session_id=source_session_id,
    )
    host = runtime.application_host
    if host is None:  # defensive: runtime construction guarantees this owner
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
