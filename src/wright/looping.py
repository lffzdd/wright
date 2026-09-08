"""会话内定时重跑：纯内存，不进 checkpoint，会话结束即消失。"""

from __future__ import annotations

import math
import queue
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

MAX_LOOPS = 20
MIN_INTERVAL_SECONDS = 5.0
MAX_NAME_CHARS = 200
MAX_PROMPT_CHARS = 4_000

LoopCommand = Literal["create", "list", "stop"]


class LoopError(ValueError):
    pass


@dataclass
class LoopRecord:
    id: str
    name: str
    prompt: str
    interval_seconds: float
    created_at: float
    next_due_at: float
    tick_count: int = 0
    pending: bool = False

    def snapshot(self) -> LoopRecord:
        return LoopRecord(
            id=self.id,
            name=self.name,
            prompt=self.prompt,
            interval_seconds=self.interval_seconds,
            created_at=self.created_at,
            next_due_at=self.next_due_at,
            tick_count=self.tick_count,
            pending=self.pending,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "interval_seconds": self.interval_seconds,
            "created_at": self.created_at,
            "next_due_at": self.next_due_at,
            "tick_count": self.tick_count,
        }


class SessionLoopRegistry:
    """In-session recurring prompts. One scheduler thread, never Agent.

    只往 event_queue 塞 LOOP_DUE；真正跑 Agent 仍只允许 REPL 线程。
    Agent 忙碌期间错过的多个周期合并成一次触发，不补跑。
    """

    def __init__(
        self,
        event_queue: queue.Queue[tuple[str, object]],
        agent_idle: threading.Event,
        *,
        max_loops: int = MAX_LOOPS,
        min_interval: float = MIN_INTERVAL_SECONDS,
    ) -> None:
        if max_loops < 1:
            raise ValueError("max_loops must be >= 1")
        if min_interval <= 0:
            raise ValueError("min_interval must be > 0")
        self.event_queue = event_queue
        self._agent_idle = agent_idle
        self.max_loops = int(max_loops)
        self.min_interval = float(min_interval)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._loops: dict[str, LoopRecord] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="wright-session-loop",
                daemon=True,
            )
            self._thread.start()

    def close(self, *, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        self._wake.set()
        if thread is not None:
            thread.join(timeout=timeout)

    def create(
        self,
        *,
        prompt: str,
        interval_seconds: float,
        name: str = "",
        now: float | None = None,
    ) -> LoopRecord:
        prompt = _bounded(prompt, "prompt", MAX_PROMPT_CHARS)
        name = _optional_name(name, prompt)
        interval = _finite_number(interval_seconds, "interval")
        if interval < self.min_interval:
            raise LoopError(
                f"interval must be >= {self.min_interval:g} seconds "
                f"(got {interval:g})"
            )
        now = time.time() if now is None else float(now)
        with self._lock:
            self._ensure_open()
            if len(self._loops) >= self.max_loops:
                raise LoopError(
                    f"at most {self.max_loops} in-session loops per session"
                )
            record = LoopRecord(
                id=f"loop_{secrets.token_hex(4)}",
                name=name,
                prompt=prompt,
                interval_seconds=interval,
                created_at=now,
                next_due_at=now + interval,
            )
            self._loops[record.id] = record
            snapshot = record.snapshot()
        self._wake.set()
        return snapshot

    def list_loops(self) -> list[LoopRecord]:
        with self._lock:
            return [record.snapshot() for record in self._loops.values()]

    def stop(self, loop_id: str) -> LoopRecord:
        with self._lock:
            self._ensure_open()
            record = self._loops.pop(loop_id, None)
            if record is None:
                raise LoopError(f"unknown loop_id: {loop_id}")
            snapshot = record.snapshot()
        self._wake.set()
        return snapshot

    def begin_tick(self, loop_id: str) -> LoopRecord | None:
        with self._lock:
            record = self._loops.get(loop_id)
            if record is None or not record.pending:
                return None
            record.tick_count += 1
            return record.snapshot()

    def finish_tick(self, loop_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        with self._lock:
            record = self._loops.get(loop_id)
            if record is None:
                return
            record.pending = False
            record.next_due_at = now + record.interval_seconds
        self._wake.set()

    def runtime_event(self, record: LoopRecord) -> dict[str, Any]:
        return {
            "type": "loop_due",
            "loop": {
                "id": record.id,
                "name": record.name[:MAX_NAME_CHARS],
                "prompt": record.prompt[:MAX_PROMPT_CHARS],
                "tick": record.tick_count,
                "interval_seconds": record.interval_seconds,
            },
        }

    def _run(self) -> None:
        while True:
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
                next_due = self._next_due_locked()
            if next_due is None:
                self._wake.wait()
                continue
            delay = next_due - time.time()
            if delay > 0 and self._wake.wait(timeout=delay):
                continue
            if not self._wait_until_idle():
                return
            with self._lock:
                if self._closed:
                    return
                due_ids = [
                    loop_id
                    for loop_id, record in self._loops.items()
                    if not record.pending and record.next_due_at <= time.time()
                ]
            for loop_id in due_ids:
                self._enqueue_due(loop_id)

    def _next_due_locked(self) -> float | None:
        due_times = [
            record.next_due_at
            for record in self._loops.values()
            if not record.pending
        ]
        return min(due_times) if due_times else None

    def _wait_until_idle(self) -> bool:
        while True:
            if self._closed:
                return False
            if self._agent_idle.wait(timeout=0.2):
                return not self._closed

    def _enqueue_due(self, loop_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            record = self._loops.get(loop_id)
            if record is None or record.pending:
                return
            record.pending = True
        self.event_queue.put(("LOOP_DUE", loop_id))

    def _ensure_open(self) -> None:
        if self._closed:
            raise LoopError("session loop registry is closed")


def parse_loop_command(value: str) -> tuple[LoopCommand, Any]:
    """Parse `/loop ...` into ('list', None) | ('stop', id) | ('create', (interval, prompt))."""
    usage = "用法: /loop <interval> <prompt>  |  /loop list  |  /loop stop <id>"
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError(usage)
    verb = parts[1].lower()
    if verb == "list":
        if len(parts) != 2:
            raise ValueError(usage)
        return "list", None
    if verb == "stop":
        if len(parts) < 3 or not parts[2].strip():
            raise ValueError("用法: /loop stop <id>")
        return "stop", parts[2].strip().split()[0]
    if len(parts) < 3 or not parts[2].strip():
        raise ValueError(usage)
    return "create", (parse_interval(parts[1]), parts[2].strip())


def parse_interval(token: str) -> float:
    raw = str(token).strip().lower()
    if not raw:
        raise ValueError("interval must be a duration such as 30s, 5m, 2h, or 10")
    unit = 1.0
    if raw[-1] in {"s", "m", "h", "d"} and len(raw) > 1 and raw[-2].isdigit():
        unit = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[raw[-1]]
        raw = raw[:-1]
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            "interval must be a duration such as 30s, 5m, 2h, or 10"
        ) from exc
    return _finite_number(value * unit, "interval")


def _optional_name(name: str, prompt: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        cleaned = prompt[:80]
    return _bounded(cleaned, "name", MAX_NAME_CHARS)


def _bounded(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise LoopError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise LoopError(f"{field} must be non-empty")
    if len(cleaned) > limit:
        raise LoopError(f"{field} exceeds {limit} chars")
    return cleaned


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoopError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise LoopError(f"{field} must be a finite number")
    if number <= 0:
        raise LoopError(f"{field} must be > 0")
    return number
