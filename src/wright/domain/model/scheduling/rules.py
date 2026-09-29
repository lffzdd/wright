"""Pure checks and clock arithmetic for job definitions.

File snapshots and web probes stay next to their I/O. Host capacity and
directory leases stay in the runtime that owns them.
"""

from __future__ import annotations

from .records import RecoveryPolicy, TriggerSpec


def checked_recovery(
    recovery_policy: str,
    max_retries: int,
    retry_delay_seconds: float,
) -> tuple[RecoveryPolicy, int, float]:
    """Validate the retry settings shared by create and update."""

    if recovery_policy not in {"manual", "retry"}:
        raise ValueError("recovery_policy must be manual or retry")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 20
    ):
        raise ValueError("max_retries must be between 0 and 20")
    if (
        isinstance(retry_delay_seconds, bool)
        or not isinstance(retry_delay_seconds, (int, float))
        or retry_delay_seconds < 0
        or retry_delay_seconds > 86_400
    ):
        raise ValueError("retry_delay_seconds must be between 0 and 86400")
    policy: RecoveryPolicy = "retry" if recovery_policy == "retry" else "manual"
    return policy, int(max_retries), float(retry_delay_seconds)


def initial_next_run(trigger: TriggerSpec, now: float) -> float | None:
    """First wall-clock due time. Persisted epoch seconds, not a monotonic clock."""

    if trigger.type == "once":
        return float(trigger.run_at or 0)
    if trigger.type == "interval":
        return (
            float(trigger.start_at)
            if trigger.start_at is not None
            else now + float(trigger.every_seconds or 0)
        )
    if trigger.type == "web_change":
        return now
    return None


def advance_interval(due: float, every_seconds: float, now: float) -> float:
    next_run = float(due)
    step = float(every_seconds)
    while next_run <= now:
        next_run += step
    return next_run


def resume_next_run(trigger: TriggerSpec, now: float) -> float | None:
    if trigger.type == "once":
        return max(now, float(trigger.run_at or now))
    if trigger.type == "interval":
        return now + float(trigger.every_seconds or 0)
    if trigger.type == "web_change":
        return now
    return None


def schedule_retry(
    *,
    recovery_policy: str,
    attempt: int,
    max_retries: int,
    blocked_by_effects: bool,
) -> bool:
    """Whether a failed or interrupted run may be queued again.

    The caller decides what counts as a blocking effect. Finishing a run
    blocks on any tool that left ``intended``. Crash recovery blocks only on
    tools that had already started. Both refuse to replay those effects.
    """

    return (
        not blocked_by_effects
        and recovery_policy == "retry"
        and attempt <= max_retries
    )
