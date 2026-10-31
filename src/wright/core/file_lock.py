"""Exclusive process locks for stable sidecar files on Windows and POSIX."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO

import portalocker


class FileLockUnavailable(BlockingIOError):
    """A non-blocking acquisition found another owner."""


class FileLock:
    """Own an OS lock until release or process exit.

    Callers create the parent directory and use a separate thread lock when
    sharing a critical section between threads. The sidecar stays on disk:
    deleting it could let two processes lock different files at the same path.
    """

    def __init__(self, path: Path, *, blocking: bool = True) -> None:
        self.path = path
        self.blocking = blocking
        self._handle: BinaryIO | None = None

    def acquire(self) -> FileLock:
        if self._handle is not None:
            return self
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            while True:
                try:
                    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    break
                except portalocker.AlreadyLocked as exc:
                    if not self.blocking:
                        raise FileLockUnavailable(f"file lock already held: {self.path}") from exc
                    # msvcrt's blocking primitive has a finite retry limit.
                    # Poll only contention, preserving flock's indefinite wait
                    # without retrying unrelated I/O errors.
                    time.sleep(0.05)
            self._handle = handle
        except BaseException:
            handle.close()
            raise
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            portalocker.unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
