"""Windows account bootstrap and per-invocation resource capability leases."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import wright.infrastructure.runtime.windows_native as native
import wright.infrastructure.runtime.windows_wfp as wfp

from ...core.paths import wright_home
from ..file_lock import FileLock
from .sandbox_profile import SandboxUnavailable, clean_environment, profile_paths
from .types import ExecutionPath

_LOCK = threading.RLock()
_READ = 0x1200A9
_MODIFY = 0x1301BF
_WRITE = 0xD0156


def runtime_dir() -> Path:
    return wright_home() / "sandbox" / "windows"


def _read() -> dict:
    path = runtime_dir() / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"state": "setup_required"}


def _write_state(state):
    path = runtime_dir() / "state.json"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _setup_running(state):
    pid = state.get("bootstrap_pid")
    if pid:
        return native.process_identity(pid) == state.get("bootstrap_started")
    return time.time() - state.get("started_at", 0) < 300


def status() -> dict:
    try:
        state = _read()
        if state.get("state") == "initializing" and not _setup_running(state):
            state.update(state="failed", error="Administrator setup did not finish; retry or clean up the journaled changes")
        available = state.get("state") == "ready" and (runtime_dir() / "credentials.bin").is_file()
        if available:
            wfp.verify(state["network"], state["account_sids"]["offline"])
        return {"platform": "win32", "provider": "Windows restricted token", "available": available,
                "state": state["state"], "detail": state.get("error", ""), "requires_admin": True}
    except Exception as error:
        return {"platform": "win32", "provider": "Windows restricted token", "available": False,
                "state": "failed", "detail": str(error), "requires_admin": True}


def _safe_runtime_roots() -> list[str]:
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    roots = [Path(os.environ["SystemRoot"]), Path(os.environ.get("ProgramFiles", "C:/Program Files")),
             executable.parent, Path(sys.base_prefix)]
    home = Path.home().resolve()
    for name in ("git", "node", "pwsh"):
        path = shutil.which(name)
        if path:
            roots.append(Path(path).resolve().parent)
    return list(dict.fromkeys(str(path.resolve()) for path in roots if path.is_dir() and path.resolve() != home
                              and path.resolve() != Path(path.anchor)))


def initialize(*, cleanup: bool = False, elevated: bool = False) -> dict:
    """Explicit UI/CLI action; UAC is requested only by this bootstrap."""
    if sys.getwindowsversion().build < 22000:
        raise SandboxUnavailable("The Windows sandbox requires Windows 11")
    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    owner = native.current_sid()
    native.private_directory(str(root), owner)
    with _LOCK, FileLock(root / "setup.lock"):
        state = _read()
        if state.get("state") == "initializing" and _setup_running(state):
            return status()
        recover_leases()
        if _leases():
            raise SandboxUnavailable("Stop active Shell commands before changing sandbox accounts or network rules")
        if not cleanup and not state.get("accounts"):
            suffix = hashlib.sha256(owner.encode()).hexdigest()[:8]
            state = {"version": 1, "owner": owner, "accounts": {"offline": f"WrightOff_{suffix}", "online": f"WrightOn_{suffix}"},
                     "runtime_sid": f"S-1-5-21-{secrets.randbits(30)}-{secrets.randbits(30)}-{secrets.randbits(30)}-1001",
                     "runtime_roots": _safe_runtime_roots(),
                     "protected_runtime_paths": [str(Path(__file__).parent.parent / "config" / "permission_settings.json")]}
            passwords = {kind: secrets.token_urlsafe(36) + "aA1!" for kind in state["accounts"]}
            (root / "credentials.bin").write_bytes(native.protect(json.dumps(passwords).encode(), machine=True))
        if cleanup and not state.get("accounts"):
            return status()
        state["runtime_roots"] = list(dict.fromkeys((*state.get("runtime_roots", []), *_safe_runtime_roots())))
        state.pop("bootstrap_pid", None)
        state.pop("bootstrap_started", None)
        for name in ("windows_setup.py", "windows_runner.py", "windows_native.py", "windows_wfp.py"):
            shutil.copyfile(Path(__file__).with_name(name), root / name)
        state["state"] = "initializing"
        state["started_at"] = time.time()
        _write_state(state)
        interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        argv = [interpreter, "-E", "-s", "-S", str(root / "windows_setup.py"), str(root), "cleanup" if cleanup else "setup"]
        # Standalone runners import only the two neighboring trusted modules.
        try:
            if elevated:
                result = subprocess.run(argv, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise SandboxUnavailable(_read().get("error", "Windows sandbox setup failed"))
            else:
                shell = ctypes.WinDLL("shell32", use_last_error=True)
                execute = native.bind(shell, "ShellExecuteW", [native.w.HWND, native.w.LPCWSTR, native.w.LPCWSTR,
                                     native.w.LPCWSTR, native.w.LPCWSTR, ctypes.c_int], ctypes.c_ssize_t)
                result = execute(None, "runas", interpreter, subprocess.list2cmdline(argv[1:]), str(root), 0)
                if result <= 32:
                    raise SandboxUnavailable("Administrator initialization was not completed; retry setup")
        except Exception as error:
            state.update(state="failed", error=str(error))
            _write_state(state)
            raise
    return status()


def _lease_file() -> Path:
    return runtime_dir() / "leases.json"


def _leases() -> dict:
    path = _lease_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _write_leases(data):
    path = _lease_file()
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _cleanup_lease(data, key):
    lease = data[key]
    for path in lease["paths"]:
        if not Path(path).exists():
            continue
        native.acl(path, lease["capability"], 0, remove=True)
        if path in lease["worker_paths"] and not any(other_key != key and path in other["worker_paths"] and other["worker"] == lease["worker"]
                   for other_key, other in data.items()):
            native.acl(path, lease["worker"], 0, remove=True)
    del data[key]


def recover_leases() -> None:
    """Job close kills children after a crash; remove only our journaled ACEs."""
    with _LOCK, FileLock(runtime_dir() / "leases.lock"):
        data = _leases()
        for key, lease in list(data.items()):
            # PID plus creation time prevents PID reuse from retaining stale ACLs.
            from .process_group import process_start
            if list(process_start(lease["pid"]) or ()) != lease["started"]:
                _cleanup_lease(data, key)
        _write_leases(data)


class WindowsProcessHandle:
    def __init__(self, process, job, lease, scratch, cwd_file):
        self.process, self.job, self.lease = process, job, lease
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
        with _LOCK, FileLock(runtime_dir() / "leases.lock"):
            data = _leases()
            if self.lease in data:
                _cleanup_lease(data, self.lease)
                _write_leases(data)


def launch(command, grant, scratch: Path, cwd_file: Path):
    with _LOCK, FileLock(runtime_dir() / "setup.lock"):
        return _launch(command, grant, scratch, cwd_file)


def _launch(command, grant, scratch: Path, cwd_file: Path):
    readiness = status()
    if not readiness["available"]:
        raise SandboxUnavailable(readiness["detail"] or "Initialize the Windows sandbox before running commands")
    recover_leases()
    state = _read()
    kind = "online" if grant.network_enabled else "offline"
    worker = state["account_sids"][kind]
    passwords = json.loads(native.protect((runtime_dir() / "credentials.bin").read_bytes(), decrypt=True))
    capability = f"S-1-5-21-{secrets.randbits(30)}-{secrets.randbits(30)}-{secrets.randbits(30)}-1002"
    readable, writable = profile_paths(grant)
    permissions = {str(path.resolve()): _READ for path in readable}
    permissions.update({str(path.resolve()): _MODIFY for path in (*writable, scratch)})
    for path in grant.shell_readonly:
        permissions[path.value] = _READ
    # Protected paths are materialized by storage before grants can be issued.
    # Runtime writes are denied to the fixed restricting SID during bootstrap.
    # Daily execution must never require changing system ACLs as administrator.
    denied = {path.value: _WRITE for path in grant.shell_readonly if not any(Path(path.value).is_relative_to(Path(root)) for root in state["runtime_roots"])}
    bootstrap_protected = {str(Path(path).resolve()).casefold() for path in state.get("protected_runtime_paths", [])}
    denied.update({path.value: 0x1F01FF for path in grant.blocked_paths
                   if Path(path.value).exists() and str(Path(path.value).resolve()).casefold() not in bootstrap_protected})
    env = clean_environment()
    env.update(HOME=str(scratch), USERPROFILE=str(scratch), APPDATA=str(scratch), LOCALAPPDATA=str(scratch),
               TEMP=str(scratch), TMP=str(scratch))
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise SandboxUnavailable("Native PowerShell is unavailable")
    request = {"command": command, "cwd": grant.cwd.value, "scratch": str(scratch), "cwd_file": str(cwd_file),
               "output": str(scratch / "output.log"), "shell": shell, "environment": env,
               "identities": [state["runtime_sid"], capability], "owners": [worker, state["owner"]]}
    request_path = scratch / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    job = native.new_job()
    lease_id = uuid4().hex
    process = None
    try:
        with _LOCK, FileLock(runtime_dir() / "leases.lock"):
            data = _leases()
            from .process_group import process_start
            data[lease_id] = {"pid": os.getpid(), "started": list(process_start(os.getpid()) or ()), "worker": worker,
                              "capability": capability, "paths": list(dict.fromkeys((*permissions, *denied))), "worker_paths": [path for path in permissions if not any(Path(path).is_relative_to(Path(root)) for root in state["runtime_roots"])]}
            _write_leases(data)  # Journal before ACL mutation, including crash recovery.
            for path, mask in permissions.items():
                if path in data[lease_id]["worker_paths"]:
                    native.acl(path, worker, mask)
                native.acl(path, capability, mask)
            for path, mask in denied.items():
                native.acl(path, capability, mask, deny=True)
        interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        argv = [interpreter, "-E", "-s", "-S", str(runtime_dir() / "windows_runner.py"), str(request_path)]
        process = native.logon_process(state["accounts"][kind], passwords[kind], interpreter, subprocess.list2cmdline(argv), str(scratch))
        native.assign_job(job, process.process)
        native.resume(process.thread)
        return WindowsProcessHandle(process, job, lease_id, scratch, cwd_file)
    except Exception:
        native.terminate_job(job)
        if process is not None:
            stop = native.bind(native.kernel, "TerminateProcess", [native.w.HANDLE, native.w.UINT])
            stop(process.process, 1)
            native.close(process.thread)
            native.close(process.process)
        native.close(job)
        with _LOCK, FileLock(runtime_dir() / "leases.lock"):
            data = _leases()
            if lease_id in data:
                _cleanup_lease(data, lease_id)
                _write_leases(data)
        raise
