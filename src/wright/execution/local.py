"""Local implementation of the replaceable execution boundary."""

from __future__ import annotations

import json
import os
import selectors
import shlex
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from pathlib import Path

from .types import DirectoryEntry, ExecutionPath, FileMetadata, SearchMatch


class LocalProcessHandle:
    """Adapt a local subprocess to :class:`ProcessHandle`."""

    def __init__(self, process: subprocess.Popen, backend: LocalExecutionBackend, cwd_file: Path):
        self._process = process
        self._backend = backend
        self._cwd_file = cwd_file
        self._cwd_result: ExecutionPath | None = None
        self._cwd_consumed = False

    def iter_output(self) -> Iterator[str]:
        if self._process.stdout is None:
            return iter(())
        return iter(self._process.stdout)

    def wait(self, timeout: float | None = None) -> int:
        result = self._process.wait(timeout=timeout)
        self._consume_cwd_result()
        return result

    def poll(self) -> int | None:
        value = self._process.poll()
        if value is not None:
            self._consume_cwd_result()
        return value

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def terminate(self) -> None:
        _terminate_process_tree(self._process)

    def cwd_result(self) -> ExecutionPath | None:
        self._consume_cwd_result()
        return self._cwd_result

    def _consume_cwd_result(self) -> None:
        if self._cwd_consumed:
            return
        self._cwd_consumed = True
        try:
            raw = self._cwd_file.read_text(encoding="utf-8").strip()
            candidate = Path(raw).resolve()
            if candidate.is_dir():
                self._cwd_result = self._backend._path_from_local(candidate)
        except (OSError, ValueError):
            self._cwd_result = None
        finally:
            try:
                self._cwd_file.unlink()
            except FileNotFoundError:
                pass


class LocalExecutionBackend:
    """Local filesystem/process implementation.

    This class intentionally knows nothing about permission policy or session
    access roots. ``AuthorizedExecution`` performs per-invocation checks; this
    backend only resolves and performs environment operations.
    """

    environment_id = "local"

    def __init__(self, workspace_dir: Path, cwd_provider: Callable[[], Path]):
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()
        self._cwd_provider = cwd_provider

    def _path_from_local(self, path: Path) -> ExecutionPath:
        return ExecutionPath(self.environment_id, str(path.resolve()))

    def _local(self, path: ExecutionPath) -> Path:
        if path.environment_id != self.environment_id:
            raise ValueError("execution path belongs to another environment")
        return Path(self.revalidate_path(path).value)

    def cwd(self) -> ExecutionPath:
        return self._path_from_local(self._cwd_provider())

    def resolve_path(
        self, requested: str, *, cwd: ExecutionPath | None = None
    ) -> ExecutionPath:
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            base = Path(cwd.value) if cwd is not None else Path(self.cwd().value)
            candidate = base / candidate
        return self._path_from_local(candidate)

    def revalidate_path(self, path: ExecutionPath) -> ExecutionPath:
        if path.environment_id != self.environment_id:
            raise ValueError("execution path belongs to another environment")
        return self._path_from_local(Path(path.value))

    def display_path(self, path: ExecutionPath) -> str:
        resolved = self.revalidate_path(path)
        try:
            relative = Path(resolved.value).relative_to(self.workspace_dir)
        except ValueError:
            return resolved.value
        return str(relative) if str(relative) != "." else "."

    def metadata(self, path: ExecutionPath) -> FileMetadata:
        local = self._local(path)
        try:
            stat = local.stat()
        except FileNotFoundError:
            return FileMetadata("missing")
        if local.is_file():
            kind = "file"
        elif local.is_dir():
            kind = "directory"
        elif local.is_symlink():
            kind = "symlink"
        else:
            kind = "other"
        return FileMetadata(kind, stat.st_size, stat.st_mtime_ns)

    def read_bytes(self, path: ExecutionPath, limit: int | None = None) -> bytes:
        with self._local(path).open("rb") as source:
            return source.read() if limit is None else source.read(limit)

    def read_text(
        self, path: ExecutionPath, *, encoding: str, errors: str = "strict"
    ) -> str:
        return self._local(path).read_text(encoding=encoding, errors=errors)

    def write_text(self, path: ExecutionPath, content: str, *, encoding: str) -> None:
        self._local(path).write_text(content, encoding=encoding)

    def ensure_directory(self, path: ExecutionPath) -> None:
        self._local(path).mkdir(parents=True, exist_ok=True)

    def iter_directory(self, path: ExecutionPath) -> Iterable[DirectoryEntry]:
        root = self._local(path)
        for child in root.iterdir():
            execution_path = self._path_from_local(child)
            yield DirectoryEntry(execution_path, self.metadata(execution_path))

    def glob(self, path: ExecutionPath, pattern: str) -> Iterable[DirectoryEntry]:
        root = self._local(path)
        for child in root.glob(pattern):
            execution_path = self._path_from_local(child)
            yield DirectoryEntry(execution_path, self.metadata(execution_path))

    def iter_search_candidates(
        self,
        path: ExecutionPath,
        *,
        glob: str | None = None,
        deadline: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Iterable[ExecutionPath]:
        root = self._local(path)
        if root.is_file():
            # An explicit file target is already selected by the caller.  rg
            # applies --glob to directory traversal, not to a path supplied
            # explicitly on the command line.
            yield self._path_from_local(root)
            return
        if not root.is_dir():
            return

        argv = ["rg", "--files", "--null", "--color", "never"]
        if glob is not None:
            argv.extend(("--glob", glob))
        argv.extend(("--", str(root)))
        process = _start_rg(argv)
        try:
            pending = bytearray()
            stderr = bytearray()
            for stream_name, chunk in _iter_rg_chunks(
                process,
                deadline=deadline,
                cancellation_check=cancellation_check,
            ):
                if stream_name == "stderr":
                    stderr.extend(chunk)
                    continue
                pending.extend(chunk)
                while b"\0" in pending:
                    raw, _, pending = pending.partition(b"\0")
                    if not raw:
                        continue
                    candidate = Path(os.fsdecode(bytes(raw)))
                    if not candidate.is_absolute():
                        candidate = root / candidate
                    yield self._path_from_local(candidate)
            if pending:
                # Be tolerant of a compatible backend that omits the final
                # NUL, while keeping all bytes in the filename intact.
                candidate = Path(os.fsdecode(bytes(pending)))
                if not candidate.is_absolute():
                    candidate = root / candidate
                yield self._path_from_local(candidate)
            returncode = _rg_returncode(process)
            if returncode not in (0, 1):
                raise RuntimeError(
                    "ripgrep candidate enumeration failed: "
                    f"{os.fsdecode(stderr).strip()}"
                )
        finally:
            _close_rg_process(process)

    def search_files(
        self,
        paths: Iterable[ExecutionPath],
        pattern: str,
        *,
        case_sensitive: bool = False,
        fixed_string: bool = False,
        deadline: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Iterable[SearchMatch]:
        """Search only the already-authorized candidate files with rg."""
        path_iterator = iter(paths)
        process: subprocess.Popen | None = None
        try:
            while True:
                _check_search_controls(deadline, cancellation_check)
                batch = list(islice(path_iterator, 128))
                if not batch:
                    break
                local_paths = [str(self._local(path)) for path in batch]
                argv = ["rg", "--json", "--color", "never", "--no-heading"]
                if not case_sensitive:
                    argv.append("--ignore-case")
                if fixed_string:
                    argv.append("--fixed-strings")
                argv.extend(("-e", pattern, "--", *local_paths))
                process = _start_rg(argv)
                try:
                    pending = bytearray()
                    stderr = bytearray()
                    for stream_name, chunk in _iter_rg_chunks(
                        process,
                        deadline=deadline,
                        cancellation_check=cancellation_check,
                    ):
                        if stream_name == "stderr":
                            stderr.extend(chunk)
                            continue
                        pending.extend(chunk)
                        while b"\n" in pending:
                            raw, _, pending = pending.partition(b"\n")
                            yield from _parse_rg_json_matches(raw, self)
                    if pending:
                        yield from _parse_rg_json_matches(pending, self)
                    returncode = _rg_returncode(process)
                    if returncode not in (0, 1):
                        raise RuntimeError(
                            f"ripgrep search failed: {os.fsdecode(stderr).strip()}"
                        )
                finally:
                    _close_rg_process(process)
                    process = None
        finally:
            if process is not None:
                _close_rg_process(process)
            close = getattr(path_iterator, "close", None)
            if callable(close):
                close()

    def start_shell(self, command: str, *, cwd: ExecutionPath) -> LocalProcessHandle:
        local_cwd = self._local(cwd)
        descriptor, cwd_file = tempfile.mkstemp(prefix="wright-cwd-")
        os.close(descriptor)
        cwd_path = Path(cwd_file)
        cwd_path.unlink(missing_ok=True)
        injected = f"eval {shlex.quote(command)} && pwd -P > {shlex.quote(str(cwd_path))}"
        process = subprocess.Popen(
            ["/bin/bash", "-c", injected],
            cwd=local_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        return LocalProcessHandle(process, self, cwd_path)


def _start_rg(argv: list[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ripgrep (rg) is required for content search") from exc


def _check_search_controls(
    deadline: float | None,
    cancellation_check: Callable[[], bool] | None,
) -> None:
    if cancellation_check is not None and cancellation_check():
        raise RuntimeError("search cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("search timed out")


def _iter_rg_chunks(
    process: subprocess.Popen,
    *,
    deadline: float | None,
    cancellation_check: Callable[[], bool] | None,
) -> Iterator[tuple[str, bytes]]:
    """Read both rg pipes without blocking on either one indefinitely."""
    selector = selectors.DefaultSelector()
    streams = (("stdout", process.stdout), ("stderr", process.stderr))
    try:
        for name, stream in streams:
            if stream is None:
                continue
            fd = stream.fileno()
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ, name)

        while selector.get_map() or process.poll() is None:
            _check_search_controls(deadline, cancellation_check)
            if not selector.get_map():
                # The child may have closed both descriptors before its wait
                # status becomes visible.  Keep the control checks live while
                # avoiding an unbounded wait.
                time.sleep(_selector_wait(deadline))
                continue
            for key, _mask in selector.select(_selector_wait(deadline)):
                try:
                    chunk = os.read(key.fd, 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise RuntimeError(f"ripgrep output read failed: {exc}") from exc
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                yield key.data, chunk
    finally:
        selector.close()


def _selector_wait(deadline: float | None) -> float:
    interval = 0.05
    if deadline is None:
        return interval
    return min(interval, max(0.0, deadline - time.monotonic()))


def _rg_returncode(process: subprocess.Popen) -> int:
    returncode = process.poll()
    if returncode is not None:
        return returncode
    try:
        return process.wait(timeout=0)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ripgrep did not exit after closing its output") from exc


def _parse_rg_json_matches(
    raw: bytes, backend: LocalExecutionBackend
) -> Iterator[SearchMatch]:
    if not raw:
        return
    try:
        event = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if event.get("type") != "match":
        return
    data = event.get("data") or {}
    path_data = data.get("path") or {}
    path_text = path_data.get("text")
    lines_data = data.get("lines") or {}
    line_text = str(lines_data.get("text", "")).rstrip("\r\n")
    if not path_text:
        return
    match_path = Path(path_text)
    if not match_path.is_absolute():
        match_path = backend.workspace_dir / match_path
    execution_path = backend._path_from_local(match_path)
    line_number = int(data.get("line_number", 0))
    for submatch in data.get("submatches") or ():
        start = int(submatch.get("start", 0))
        yield SearchMatch(
            execution_path,
            line_number,
            _utf8_column(line_text, start),
            line_text,
        )


def _close_rg_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        _terminate_process_tree(process)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _utf8_column(line: str, byte_offset: int) -> int:
    prefix = line.encode("utf-8")[:byte_offset]
    return len(prefix.decode("utf-8", errors="replace")) + 1


def _terminate_process_tree(process: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return

    def send(sig: signal.Signals) -> None:
        try:
            group = os.getpgid(process.pid)
            if group == process.pid:
                os.killpg(group, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return
