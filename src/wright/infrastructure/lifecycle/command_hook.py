"""Run a configured command hook. The application decides what the result means."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ...application.lifecycle.contracts import (
    HookDecision,
    HookExecutionError,
    LifecycleConfigError,
    LifecycleEvent,
)


class CommandHook:
    """Run a configured argv command with the lifecycle event on stdin."""

    def __init__(
        self, command: Sequence[str], *, cwd: Path, timeout: float = 5.0
    ) -> None:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise LifecycleConfigError("hook command must be a non-empty argv array")
        if timeout <= 0 or timeout > 60:
            raise LifecycleConfigError("hook timeout must be in (0, 60] seconds")
        self.command = tuple(command)
        self.cwd = cwd.resolve()
        self.timeout = float(timeout)

    def __call__(self, event: LifecycleEvent) -> HookDecision:
        try:
            completed = subprocess.run(
                self.command,
                cwd=self.cwd,
                input=json.dumps(event.to_dict(), ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HookExecutionError(f"hook timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise HookExecutionError(f"hook could not start: {exc}") from exc
        if completed.returncode == 2:
            return HookDecision(
                decision="deny",
                reason=(completed.stderr.strip() or "hook blocked execution")[:2_000],
            )
        if completed.returncode != 0:
            raise HookExecutionError(
                f"hook exited {completed.returncode}: {completed.stderr.strip()[:1_000]}"
            )
        output = completed.stdout.strip()
        if not output:
            return HookDecision()
        try:
            return HookDecision.from_value(json.loads(output))
        except json.JSONDecodeError as exc:
            raise HookExecutionError(f"hook returned invalid JSON: {exc}") from exc

