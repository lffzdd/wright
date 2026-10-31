"""Shared Windows Job-owned Shell process lifecycle."""
from __future__ import annotations

import ctypes
import subprocess
import threading
from pathlib import Path

import wright.infrastructure.runtime.windows_native as native

from ...domain.gateway.runtime_types import ExecutionPath


class WindowsProcessHandle:
    def __init__(self, process, job, scratch, cwd_file):
        self.process, self.job = process, job
        self.scratch, self.cwd_file = scratch, cwd_file
        self.offset, self._code, self._cwd = 0, None, None
        self._lock = threading.RLock()

    def read_output(self, max_bytes):
        path = self.scratch / "output.log"
        if path.is_file():
            with path.open("rb") as file:
                file.seek(self.offset)
                chunk = file.read(max_bytes)
            self.offset += len(chunk)
            if chunk:
                return chunk
        error = self.scratch / "request.error"
        if error.is_file() and not self.offset:
            self.offset = 1
            return error.read_bytes()
        return None if not self.group_alive() else b""

    def poll(self):
        with self._lock:
            if self._code is None:
                code = native.w.DWORD()
                native.check(native.exit_code(self.process.process, ctypes.byref(code)))
                if code.value != 259:
                    self._code = code.value
                    if self.cwd_file.is_file():
                        self._cwd = ExecutionPath("local", str(Path(self.cwd_file.read_text(encoding="utf-8")).resolve()))
            return self._code

    @property
    def returncode(self):
        return self.poll()

    def wait(self, timeout=None):
        result = native.wait(self.process.process, 0xFFFFFFFF if timeout is None else int(timeout * 1000))
        if result == 258:
            raise subprocess.TimeoutExpired("sandbox", timeout)
        return self.poll()

    def group_alive(self):
        with self._lock:
            alive = bool(self.job and native.job_active(self.job))
            if not alive:
                self._release()
            return alive

    def terminate(self, *, grace_seconds=2.0):
        with self._lock:
            if self.job:
                native.terminate_job(self.job)
                native.wait(self.process.process, 5000)
            self.poll()
            self._release()
            return True

    def cwd_result(self):
        self.poll()
        return self._cwd

    def _release(self):
        if not self.job:
            return
        self.poll()
        native.close(self.job)
        self.job = None
        native.close(self.process.thread)
        # Keep the process handle until final poll has consumed its code.
        native.close(self.process.process)

