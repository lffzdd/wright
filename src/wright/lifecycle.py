"""Lifecycle hooks and local structured traces.

The trace is the durable fact log. Hooks are optional reactions to those facts:
observer hooks cannot affect execution, while blocking hooks may explicitly deny
or rewrite a tool input. Hook failures are recorded and fail open; only a valid
``decision: deny`` response blocks work.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

LifecycleEventName = Literal[
    "session_start",
    "runtime_event",
    "user_prompt_submit",
    "agent_start",
    "agent_stop",
    "llm_start",
    "llm_end",
    "llm_error",
    "pre_tool_use",
    "permission_decision",
    "post_tool_use",
    "tool_failure",
    "subagent_start",
    "subagent_stop",
    "pre_compact",
    "post_compact",
    "hook_result",
    "hook_error",
]

LIFECYCLE_EVENT_NAMES = frozenset(
    {
        "session_start",
        "runtime_event",
        "user_prompt_submit",
        "agent_start",
        "agent_stop",
        "llm_start",
        "llm_end",
        "llm_error",
        "pre_tool_use",
        "permission_decision",
        "post_tool_use",
        "tool_failure",
        "subagent_start",
        "subagent_stop",
        "pre_compact",
        "post_compact",
        "hook_result",
        "hook_error",
    }
)
HOOKABLE_EVENT_NAMES = LIFECYCLE_EVENT_NAMES - {"hook_result", "hook_error"}

BLOCKING_EVENTS = frozenset({"user_prompt_submit", "pre_tool_use", "agent_stop"})


class LifecycleConfigError(ValueError):
    pass


class HookExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HookDecision:
    decision: Literal["allow", "deny"] = "allow"
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str = ""

    @classmethod
    def from_value(cls, value: Any) -> HookDecision:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise HookExecutionError("hook output must be a JSON object")
        decision = value.get("decision", "allow")
        if decision not in {"allow", "deny"}:
            raise HookExecutionError("hook decision must be 'allow' or 'deny'")
        updated_input = value.get("updated_input")
        if updated_input is not None and not isinstance(updated_input, dict):
            raise HookExecutionError("hook updated_input must be an object")
        return cls(
            decision=decision,
            reason=str(value.get("reason") or "")[:2_000],
            updated_input=dict(updated_input) if updated_input is not None else None,
            additional_context=str(value.get("additional_context") or "")[:8_000],
        )


@dataclass(frozen=True)
class LifecycleEvent:
    event: LifecycleEventName
    session_id: str
    sequence: int
    timestamp: float
    payload: dict[str, Any]
    agent_task_id: str | None = None
    root_turn_id: str = ""
    event_id: str = field(default_factory=lambda: uuid4().hex)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event": self.event,
            "session_id": self.session_id,
            "agent_task_id": self.agent_task_id,
            "root_turn_id": self.root_turn_id,
            "payload": _bounded_value(self.payload),
        }


class TraceRecorder:
    """Thread-safe append-only JSONL recorder, one file per root session."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, event: LifecycleEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            needs_separator = False
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as existing:
                    existing.seek(-1, 2)
                    needs_separator = existing.read(1) != b"\n"
            with self.path.open("a", encoding="utf-8") as handle:
                if needs_separator:
                    # Preserve a crash-truncated fragment as an invalid row; do
                    # not concatenate the next valid event onto it.
                    handle.write("\n")
                handle.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # A process may die between write() and newline flush.
                        # Later valid records must remain readable.
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        return rows

    def last_sequence(self) -> int:
        rows = self.read()
        if not rows:
            return 0
        value = rows[-1].get("sequence", 0)
        return value if isinstance(value, int) and value >= 0 else 0


HookCallback = Callable[[LifecycleEvent], HookDecision | Mapping[str, Any] | None]


@dataclass(frozen=True)
class HookRegistration:
    event: LifecycleEventName
    callback: HookCallback
    matcher: str = "*"
    name: str = "hook"

    def matches(self, payload: Mapping[str, Any]) -> bool:
        subject = str(payload.get("tool_name") or payload.get("agent_type") or "")
        return self.matcher == "*" or fnmatchcase(subject, self.matcher)


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


class LifecycleManager:
    """Dispatch lifecycle events to a trace and optional matched hooks."""

    def __init__(self, session_id: str, recorder: TraceRecorder | None = None) -> None:
        self.session_id = session_id
        self.recorder = recorder
        self._hooks: dict[str, list[HookRegistration]] = {}
        self._sequence = recorder.last_sequence() if recorder is not None else 0
        self._lock = threading.RLock()

    def register(self, registration: HookRegistration) -> None:
        with self._lock:
            self._hooks.setdefault(registration.event, []).append(registration)

    def emit(
        self,
        event: LifecycleEventName,
        payload: Mapping[str, Any] | None = None,
        *,
        agent_task_id: str | None = None,
        root_turn_id: str = "",
    ) -> HookDecision:
        current_payload = dict(payload or {})
        with self._lock:
            lifecycle_event = self._new_event(
                event,
                current_payload,
                agent_task_id=agent_task_id,
                root_turn_id=root_turn_id,
            )
            self._record(lifecycle_event)
            hooks = list(self._hooks.get(event, ()))

        aggregate = HookDecision()
        for registration in hooks:
            if not registration.matches(current_payload):
                continue
            try:
                decision = HookDecision.from_value(
                    registration.callback(lifecycle_event)
                )
            except Exception as exc:  # hook 失败 fail-open，已记入 hook_error
                self._record_hook_error(lifecycle_event, registration.name, exc)
                continue
            self._record_hook_result(lifecycle_event, registration.name, decision)
            if decision.updated_input is not None:
                current_payload["arguments"] = dict(decision.updated_input)
                aggregate = HookDecision(
                    decision=aggregate.decision,
                    reason=aggregate.reason,
                    updated_input=dict(decision.updated_input),
                    additional_context=_join_context(
                        aggregate.additional_context, decision.additional_context
                    ),
                )
            elif decision.additional_context:
                aggregate = HookDecision(
                    decision=aggregate.decision,
                    reason=aggregate.reason,
                    updated_input=aggregate.updated_input,
                    additional_context=_join_context(
                        aggregate.additional_context, decision.additional_context
                    ),
                )
            if decision.decision == "deny" and event in BLOCKING_EVENTS:
                return HookDecision(
                    decision="deny",
                    reason=decision.reason or f"blocked by hook {registration.name}",
                    updated_input=aggregate.updated_input,
                    additional_context=aggregate.additional_context,
                )
        return aggregate

    def _new_event(
        self,
        event: LifecycleEventName,
        payload: dict[str, Any],
        *,
        agent_task_id: str | None,
        root_turn_id: str,
    ) -> LifecycleEvent:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return LifecycleEvent(
            event=event,
            session_id=self.session_id,
            sequence=sequence,
            timestamp=time.time(),
            payload=payload,
            agent_task_id=agent_task_id,
            root_turn_id=root_turn_id,
        )

    def _record(self, event: LifecycleEvent) -> None:
        if self.recorder is not None:
            self.recorder.append(event)

    def _record_hook_error(
        self,
        source: LifecycleEvent,
        hook_name: str,
        error: Exception,
    ) -> None:
        with self._lock:
            event = self._new_event(
                "hook_error",
                {
                    "source_event": source.event,
                    "source_event_id": source.event_id,
                    "hook": hook_name,
                    "error": f"{type(error).__name__}: {error}"[:2_000],
                },
                agent_task_id=source.agent_task_id,
                root_turn_id=source.root_turn_id,
            )
            self._record(event)

    def _record_hook_result(
        self,
        source: LifecycleEvent,
        hook_name: str,
        decision: HookDecision,
    ) -> None:
        with self._lock:
            event = self._new_event(
                "hook_result",
                {
                    "source_event": source.event,
                    "source_event_id": source.event_id,
                    "hook": hook_name,
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "updated_input": decision.updated_input,
                    "additional_context": decision.additional_context,
                },
                agent_task_id=source.agent_task_id,
                root_turn_id=source.root_turn_id,
            )
            self._record(event)


def load_lifecycle_manager(
    workspace_dir: Path,
    session_id: str,
    *,
    config_path: Path | None = None,
    trace_dir: Path | None = None,
) -> LifecycleManager:
    """Create tracing and load only an explicitly selected command-hook file."""
    workspace = workspace_dir.resolve()
    traces = (trace_dir or (workspace / ".wright_traces")).resolve()
    manager = LifecycleManager(
        session_id,
        TraceRecorder(traces / f"{session_id}.jsonl"),
    )
    if config_path is None:
        return manager
    path = config_path.resolve()
    if not path.exists():
        raise LifecycleConfigError(f"hook config does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleConfigError(f"cannot load hook config {path}: {exc}") from exc
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        raise LifecycleConfigError("hook config requires an object field 'hooks'")
    for event, rows in hooks.items():
        if event not in HOOKABLE_EVENT_NAMES:
            raise LifecycleConfigError(f"unknown hook event: {event}")
        if not isinstance(rows, list):
            raise LifecycleConfigError(f"hooks.{event} must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise LifecycleConfigError(f"hooks.{event}[{index}] must be an object")
            command = row.get("command")
            if not isinstance(command, list):
                raise LifecycleConfigError(
                    f"hooks.{event}[{index}].command must be an argv array"
                )
            name = str(row.get("name") or f"{event}[{index}]")[:100]
            try:
                timeout = float(row.get("timeout", 5))
            except (TypeError, ValueError) as exc:
                raise LifecycleConfigError(
                    f"hooks.{event}[{index}].timeout must be a number"
                ) from exc
            manager.register(
                HookRegistration(
                    event=event,
                    callback=CommandHook(
                        command,
                        cwd=workspace,
                        timeout=timeout,
                    ),
                    matcher=str(row.get("matcher") or "*"),
                    name=name,
                )
            )
    return manager


def _join_context(left: str, right: str) -> str:
    if not right:
        return left
    return f"{left}\n{right}".strip()[:8_000]


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "[trace depth limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 8_000 else value[:8_000] + "[truncated]"
    if isinstance(value, Mapping):
        bounded = {}
        for key, item in list(value.items())[:100]:
            name = str(key)[:200]
            if any(
                marker in name.lower()
                for marker in (
                    "api_key",
                    "apikey",
                    "password",
                    "passwd",
                    "secret",
                    "authorization",
                    "cookie",
                    "access_token",
                    "refresh_token",
                )
            ):
                bounded[name] = "[redacted]"
            else:
                bounded[name] = _bounded_value(item, depth=depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth + 1) for item in value[:100]]
    if hasattr(value, "to_dict"):
        try:
            return _bounded_value(value.to_dict(), depth=depth + 1)
        except Exception:  # 脱敏失败则退回 repr
            pass
    return repr(value)[:2_000]
