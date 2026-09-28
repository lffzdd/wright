"""Serialize top-level work that shares one execution directory.

Interactive turns, durable automations, background commands, and subagents
use one queue per resolved execution root. A child of work that already holds
the directory inherits that occupancy immediately. Waiting releases this
module's lock, so another session can still be queried or cancelled.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class DirectoryWaitCancelled(RuntimeError):
    """The waiter left the queue. The current holder is unchanged."""

    def __init__(self, holder_id: str) -> None:
        self.holder_id = holder_id
        super().__init__(f"directory wait cancelled: {holder_id}")


@dataclass
class DirectoryLease:
    """One live occupancy of an execution directory."""

    lease_id: str
    execution_root: str
    kind: str
    holder_id: str
    session_id: str
    parent_lease_id: str | None
    label: str

    def release(self) -> None:
        release = getattr(self, "_release", None)
        if release is not None:
            release(self.lease_id)


@dataclass(frozen=True)
class DirectoryBinding:
    coordinator: DirectoryExecutionCoordinator
    lease: DirectoryLease


_binding: ContextVar[DirectoryBinding | None] = ContextVar(
    "wright_directory_binding", default=None
)


def current_directory_binding() -> DirectoryBinding | None:
    return _binding.get()


def bind_directory_work(coordinator: DirectoryExecutionCoordinator, lease: DirectoryLease) -> Token:
    return _binding.set(DirectoryBinding(coordinator, lease))


def reset_directory_work(token: Token) -> None:
    _binding.reset(token)


@dataclass
class _Waiter:
    holder_id: str
    session_id: str
    kind: str
    label: str
    ready: threading.Event
    lease: DirectoryLease | None = None
    cancelled: bool = False
    reason: str = ""


class DirectoryExecutionCoordinator:
    """FIFO directory leases. The lock is never held while a waiter sleeps."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holders: dict[str, dict[str, DirectoryLease]] = {}
        self._queues: dict[str, deque[_Waiter]] = {}
        self._by_id: dict[str, DirectoryLease] = {}
        self._waiters: dict[tuple[str, str], _Waiter] = {}

    def acquire(
        self,
        execution_root: Path | str,
        *,
        kind: str,
        holder_id: str,
        session_id: str,
        label: str,
        parent_lease_id: str | None = None,
        cancel: threading.Event | None = None,
        on_queued: Callable[[str], None] | None = None,
    ) -> DirectoryLease:
        root = _normalize(execution_root)
        with self._lock:
            inherited = self._inherited_locked(root, parent_lease_id)
            if inherited is not None or self._can_grant_locked(root):
                return self._grant_locked(
                    root, kind=kind, holder_id=holder_id, session_id=session_id,
                    parent_lease_id=parent_lease_id if inherited is not None else None,
                    label=label,
                )
            if cancel is not None and cancel.is_set():
                raise DirectoryWaitCancelled(holder_id)
            waiter = _Waiter(
                holder_id=holder_id, session_id=session_id, kind=kind, label=label,
                ready=threading.Event(), reason=self._reason_locked(root),
            )
            self._queues.setdefault(root, deque()).append(waiter)
            self._waiters[(session_id, holder_id)] = waiter
            reason = waiter.reason
        if on_queued is not None:
            on_queued(reason)
        while not waiter.ready.wait(timeout=0.05):
            if cancel is not None and cancel.is_set():
                self._drop_waiter(root, waiter)
                raise DirectoryWaitCancelled(holder_id)
        leaked: DirectoryLease | None = None
        with self._lock:
            self._waiters.pop((session_id, holder_id), None)
            if waiter.cancelled or waiter.lease is None:
                leaked = waiter.lease
                waiter.lease = None
            else:
                return waiter.lease
        if leaked is not None:
            self.release(leaked.lease_id)
        raise DirectoryWaitCancelled(holder_id)

    def retain(
        self,
        parent_lease_id: str,
        *,
        kind: str,
        holder_id: str,
        session_id: str,
        label: str,
    ) -> DirectoryLease:
        """Keep the directory occupied after the parent lease is released.

        This does not enter the queue and cannot deadlock a parent that is
        waiting for the child: the parent already holds the directory.
        """

        with self._lock:
            parent = self._by_id.get(parent_lease_id)
            if parent is None:
                raise RuntimeError("parent directory lease is no longer held")
            return self._grant_locked(
                parent.execution_root, kind=kind, holder_id=holder_id,
                session_id=session_id, parent_lease_id=parent_lease_id, label=label,
            )

    def release(self, lease_id: str) -> None:
        grant: tuple[_Waiter, DirectoryLease] | None = None
        with self._lock:
            lease = self._by_id.pop(lease_id, None)
            if lease is None:
                return
            holders = self._holders.get(lease.execution_root)
            if holders is not None:
                holders.pop(lease_id, None)
                if not holders:
                    self._holders.pop(lease.execution_root, None)
            if self._holders.get(lease.execution_root):
                return
            queue = self._queues.get(lease.execution_root)
            if not queue:
                self._queues.pop(lease.execution_root, None)
                return
            waiter = queue.popleft()
            while waiter.cancelled:
                if not queue:
                    self._queues.pop(lease.execution_root, None)
                    return
                waiter = queue.popleft()
            if not queue:
                self._queues.pop(lease.execution_root, None)
            granted = self._grant_locked(
                lease.execution_root, kind=waiter.kind, holder_id=waiter.holder_id,
                session_id=waiter.session_id, parent_lease_id=None, label=waiter.label,
            )
            waiter.lease = granted
            grant = (waiter, granted)
        if grant is not None:
            grant[0].ready.set()

    def cancel_waiters(
        self, *, session_id: str | None = None, holder_id: str | None = None,
    ) -> int:
        """Remove queued waiters. Holders keep running."""

        cancelled = 0
        with self._lock:
            for root, queue in list(self._queues.items()):
                kept: deque[_Waiter] = deque()
                for waiter in queue:
                    matched = (
                        (session_id is None or waiter.session_id == session_id)
                        and (holder_id is None or waiter.holder_id == holder_id)
                    )
                    if matched:
                        waiter.cancelled = True
                        waiter.ready.set()
                        self._waiters.pop((waiter.session_id, waiter.holder_id), None)
                        cancelled += 1
                    else:
                        kept.append(waiter)
                if kept:
                    self._queues[root] = kept
                else:
                    self._queues.pop(root, None)
        return cancelled

    def occupied(self, execution_root: Path | str) -> bool:
        root = _normalize(execution_root)
        with self._lock:
            return bool(self._holders.get(root))

    def waiting_reason(self, session_id: str) -> str | None:
        with self._lock:
            for waiter in self._waiters.values():
                if waiter.session_id == session_id and not waiter.cancelled:
                    return waiter.reason or "waiting for the execution directory"
        return None

    def describe(self, execution_root: Path | str) -> dict[str, object]:
        root = _normalize(execution_root)
        with self._lock:
            holders = [
                {
                    "lease_id": lease.lease_id,
                    "kind": lease.kind,
                    "holder_id": lease.holder_id,
                    "session_id": lease.session_id,
                    "label": lease.label,
                }
                for lease in self._holders.get(root, {}).values()
            ]
            queue = [
                {
                    "position": index,
                    "kind": waiter.kind,
                    "holder_id": waiter.holder_id,
                    "session_id": waiter.session_id,
                    "label": waiter.label,
                    "reason": waiter.reason,
                }
                for index, waiter in enumerate(self._queues.get(root, ()), start=1)
            ]
        return {"execution_root": root, "holders": holders, "queue": queue}

    def _drop_waiter(self, root: str, waiter: _Waiter) -> None:
        with self._lock:
            queue = self._queues.get(root)
            if queue is not None:
                kept = deque(item for item in queue if item is not waiter)
                if kept:
                    self._queues[root] = kept
                else:
                    self._queues.pop(root, None)
            waiter.cancelled = True
            self._waiters.pop((waiter.session_id, waiter.holder_id), None)

    def _inherited_locked(self, root: str, parent_lease_id: str | None) -> DirectoryLease | None:
        if not parent_lease_id:
            return None
        parent = self._by_id.get(parent_lease_id)
        if parent is None or parent.execution_root != root:
            return None
        return parent

    def _can_grant_locked(self, root: str) -> bool:
        return not self._holders.get(root) and not self._queues.get(root)

    def _reason_locked(self, root: str) -> str:
        holders = list(self._holders.get(root, {}).values())
        if not holders:
            waiting = self._queues.get(root)
            if waiting:
                return f"waiting for {waiting[0].label} to finish in this directory"
            return "waiting for the execution directory"
        return f"waiting for {holders[0].label} to finish in this directory"

    def _grant_locked(
        self,
        root: str,
        *,
        kind: str,
        holder_id: str,
        session_id: str,
        parent_lease_id: str | None,
        label: str,
    ) -> DirectoryLease:
        lease = DirectoryLease(
            lease_id=uuid4().hex,
            execution_root=root,
            kind=kind,
            holder_id=holder_id,
            session_id=session_id,
            parent_lease_id=parent_lease_id,
            label=label,
        )
        lease._release = self.release  # type: ignore[attr-defined]
        self._by_id[lease.lease_id] = lease
        self._holders.setdefault(root, {})[lease.lease_id] = lease
        return lease


def _normalize(execution_root: Path | str) -> str:
    return str(Path(execution_root).expanduser().resolve())


__all__ = [
    "DirectoryBinding",
    "DirectoryExecutionCoordinator",
    "DirectoryLease",
    "DirectoryWaitCancelled",
    "bind_directory_work",
    "current_directory_binding",
    "reset_directory_work",
]
