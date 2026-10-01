"""Real process contention and lock cleanup on Windows and POSIX."""

from __future__ import annotations

import multiprocessing
from contextlib import contextmanager
from pathlib import Path

import portalocker
import pytest

from wright.infrastructure.file_lock import FileLock, FileLockUnavailable


def _hold_lock(path, channel):
    channel.send("waiting")
    with FileLock(Path(path)):
        channel.send("held")
        channel.recv()
    channel.close()


@contextmanager
def _lock_process(path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_lock, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "lock worker did not start"
        assert parent.recv() == "waiting"
        yield process, parent
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        parent.close()
        process.close()


def test_nonblocking_lock_rejects_another_process_and_can_be_reused(tmp_path):
    path = tmp_path / "owner.lock"
    contender = FileLock(path, blocking=False)
    with _lock_process(path) as (process, channel):
        assert channel.poll(10)
        assert channel.recv() == "held"
        with pytest.raises(FileLockUnavailable):
            contender.acquire()
        channel.send("release")
        process.join(10)
        assert process.exitcode == 0
    with contender:
        assert path.is_file()
    assert path.is_file()


def test_blocking_lock_waits_for_the_owner_to_release(tmp_path):
    path = tmp_path / "store.lock"
    owner = FileLock(path).acquire()
    try:
        with _lock_process(path) as (process, channel):
            assert not channel.poll(0.2), "worker acquired a held lock"
            owner.release()
            assert channel.poll(10), "worker did not acquire the released lock"
            assert channel.recv() == "held"
            channel.send("release")
            process.join(10)
            assert process.exitcode == 0
    finally:
        owner.release()


def test_killed_process_releases_its_lock(tmp_path):
    path = tmp_path / "crash.lock"
    with _lock_process(path) as (process, channel):
        assert channel.poll(10)
        assert channel.recv() == "held"
        process.kill()
        process.join(10)
        assert process.exitcode is not None and process.exitcode != 0
        with FileLock(path, blocking=False):
            pass


def test_exception_in_critical_section_releases_lock(tmp_path):
    path = tmp_path / "exception.lock"
    with pytest.raises(ValueError, match="write failed"), FileLock(path):
        raise ValueError("write failed")
    with FileLock(path, blocking=False):
        pass


@pytest.mark.parametrize("error", [OSError("I/O failure"), KeyboardInterrupt()])
def test_failed_acquisition_closes_handle_and_does_not_retry(tmp_path, monkeypatch, error):
    handles = []

    def fail(handle, flags):
        handles.append(handle)
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(portalocker, "lock", fail)
        with pytest.raises(type(error)):
            FileLock(tmp_path / "failure.lock").acquire()
    assert len(handles) == 1 and handles[0].closed
    with FileLock(tmp_path / "failure.lock", blocking=False):
        pass
