"""Explicit host Shell execution; never an isolation-failure fallback.

Commands retain Wright's output, cwd and process-tree ownership contracts but
run as the current account without filesystem or network isolation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ...domain.policy.permission.types import InvocationGrant
from .sandbox_profile import clean_environment
from .shell_script import bash_launch_script, powershell_launch_script


def launch(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path):
    if grant.execution_mode != "host":
        raise ValueError("Host launch requires an explicit host invocation grant")
    env = clean_environment()
    # Keep the actual account home and temp directories in host mode. Continue
    # filtering agent API credentials with the same environment allowlist.
    for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TMPDIR", "TMP", "TEMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    if sys.platform == "win32":
        return _windows_launch(command, grant, scratch, cwd_file, env)
    script = bash_launch_script(command, scratch, cwd_file)
    return subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", script],
        cwd=grant.cwd.value, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True)


def _windows_launch(command, grant, scratch, cwd_file, env):
    """Create suspended, attach a kill-on-close Job, then allow execution."""
    import ctypes
    import msvcrt

    # PowerShell writes UTF-8 cwd explicitly rather than cmd.exe's OEM output.
    import shutil

    import wright.infrastructure.runtime.windows_native as native

    from .windows_process import WindowsProcessHandle
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise RuntimeError("PowerShell is required for host Shell execution on Windows")
    script = powershell_launch_script(command, scratch, cwd_file)
    argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
    job = native.new_job()
    process = native.PROCESS()
    try:
        with (scratch / "output.log").open("wb") as output, open(os.devnull, "rb") as input_file:
            output_handle = msvcrt.get_osfhandle(output.fileno())
            input_handle = msvcrt.get_osfhandle(input_file.fileno())
            os.set_handle_inheritable(output_handle, True)
            os.set_handle_inheritable(input_handle, True)
            start = native.STARTUP()
            start.cb = ctypes.sizeof(start)
            start.flags = 0x100
            start.stdin, start.stdout, start.stderr = input_handle, output_handle, output_handle
            create = native.bind(native.kernel, "CreateProcessW", [native.w.LPCWSTR, native.w.LPWSTR,
                ctypes.c_void_p, ctypes.c_void_p, native.w.BOOL, native.w.DWORD, ctypes.c_void_p,
                native.w.LPCWSTR, ctypes.POINTER(native.STARTUP), ctypes.POINTER(native.PROCESS)])
            block = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0")
            native.check(create(None, ctypes.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None,
                True, 0x08000404, block, grant.cwd.value, ctypes.byref(start), ctypes.byref(process)))
        native.assign_job(job, process.process)
        native.check(native.resume(process.thread) != 0xFFFFFFFF)
        return WindowsProcessHandle(process, job, scratch, cwd_file)
    except BaseException:
        if process.process:
            terminate = native.bind(native.kernel, "TerminateProcess", [native.w.HANDLE, native.w.UINT])
            terminate(process.process, 1)
            native.close(process.thread)
            native.close(process.process)
        native.close(job)
        raise
