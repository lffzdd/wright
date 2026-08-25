"""Small process-lifecycle helpers shared by shell execution and task control."""

from __future__ import annotations

import os
import signal
import subprocess


def terminate_process_tree(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate a command and its descendants when it owns a process group."""
    if process.poll() is not None:
        return

    def send(sig: signal.Signals) -> None:
        try:
            process_group = os.getpgid(process.pid)
            if process_group == process.pid:
                os.killpg(process_group, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            # The caller can still observe a non-terminal task; never pretend
            # termination succeeded when the OS has not reaped the process.
            return
