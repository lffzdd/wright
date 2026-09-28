"""Dispatch lifecycle events to a trace and optional matched hooks."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from .contracts import (
    BLOCKING_EVENTS,
    HookDecision,
    HookRegistration,
    LifecycleEvent,
    LifecycleEventName,
    TraceSink,
    _join_context,
)


class LifecycleManager:
    """Dispatch lifecycle events to a trace and optional matched hooks."""

    def __init__(self, session_id: str, recorder: TraceSink | None = None) -> None:
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
