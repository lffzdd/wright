"""Ownership-checked process groups for one shell execution.

The group id is recorded when the process starts. A later signal is sent only
when that same process is still the leader, or the leader has exited and the
group still contains its original members. A recycled pid is not signaled.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import time
from functools import lru_cache

_PROC_PIDTBSDINFO = 3
_MAXCOMLEN = 16


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * _MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * _MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def _process_library() -> ctypes.CDLL:
    # This adapter is only needed when a shell process is started. Loading
    # macOS's libproc at import time prevents even Windows CLI/Web startup.
    library = ctypes.util.find_library("proc")
    if library is None:
        raise RuntimeError("the local process-group adapter requires macOS libproc")
    return ctypes.CDLL(library, use_errno=True)


def process_start(pid: int) -> tuple[int, int] | None:
    """Return the kernel start time of ``pid``, or None if that pid is gone."""
    if sys.platform == "win32":
        import wright.infrastructure.runtime.windows_native as native
        kernel = native.kernel
        open_process = native.bind(kernel, "OpenProcess", [native.w.DWORD, native.w.BOOL, native.w.DWORD], native.w.HANDLE)
        handle = open_process(0x1000, False, pid)
        if not handle:
            return None
        try:
            values = (ctypes.c_uint64 * 4)()
            times = native.bind(kernel, "GetProcessTimes", [native.w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p])
            native.check(times(handle, ctypes.byref(values, 0), ctypes.byref(values, 8), ctypes.byref(values, 16), ctypes.byref(values, 24)))
            return int(values[0]), 0
        finally:
            native.close(handle)
    if sys.platform.startswith("linux"):
        from pathlib import Path
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            return int(fields[19]), 0
        except (OSError, ValueError, IndexError):
            return None
    info = _ProcBsdInfo()
    size = _process_library().proc_pidinfo(
        ctypes.c_int(pid),
        ctypes.c_int(_PROC_PIDTBSDINFO),
        ctypes.c_uint64(0),
        ctypes.byref(info),
        ctypes.c_int(ctypes.sizeof(info)),
    )
    if int(size) < ctypes.sizeof(info) or int(info.pbi_pid) != pid:
        return None
    return int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)


def _group_members(pgid: int) -> list[int]:
    completed = subprocess.run(
        ["ps", "-ax", "-o", "pid=,pgid="],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    members: list[int] = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == pgid:
            members.append(pid)
    return members


class OwnedProcessGroup:
    """A process group created by one ``start_new_session`` spawn."""

    def __init__(self, leader_pid: int) -> None:
        self.leader_pid = leader_pid
        self.pgid = os.getpgid(leader_pid)
        started = process_start(leader_pid)
        if started is None:
            raise RuntimeError("spawned process exited before ownership was recorded")
        self._start = started

    def leader_reused(self) -> bool:
        """True when ``leader_pid`` now names a different process."""
        current = process_start(self.leader_pid)
        return current is not None and current != self._start

    def members(self) -> list[int]:
        if self.leader_reused():
            return []
        return _group_members(self.pgid)

    def alive(self) -> bool:
        return bool(self.members())

    def terminate(self, grace_seconds: float = 2.0) -> bool:
        """Signal this group. Return True only when no owned member remains.

        A recycled leader pid is left untouched. Sending SIGTERM is not
        reported as success while members are still alive.
        """
        if self.leader_reused():
            return False
        if not self.members():
            return True
        self._signal(signal.SIGTERM)
        if self._wait_until_clear(grace_seconds):
            return True
        if self.leader_reused():
            return False
        if self.members():
            self._signal(signal.SIGKILL)
        return self._wait_until_clear(grace_seconds)

    def _signal(self, sig: signal.Signals) -> None:
        if self.leader_reused():
            return
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            return

    def _wait_until_clear(self, grace_seconds: float) -> bool:
        deadline = time.monotonic() + grace_seconds
        while True:
            if self.leader_reused() or not self.members():
                return not self.leader_reused()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


__all__ = ["OwnedProcessGroup", "process_start"]
