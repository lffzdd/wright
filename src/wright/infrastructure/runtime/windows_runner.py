"""Trusted runner, copied alongside windows_native.py into protected storage."""

import json
import msvcrt
import os
import subprocess
import sys
from pathlib import Path

import windows_native as native


def run(request_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    script = Path(request["scratch"]) / "command.ps1"
    # Output capture belongs to the shell, so detached descendants inherit the
    # same output file and the controller's Job Object.
    command_file = Path(request["scratch"]) / "body.ps1"
    command_file.write_text(request["command"], encoding="utf-8-sig")
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script.write_text(
        "$ErrorActionPreference = 'Continue'\n[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n$OutputEncoding = [Console]::OutputEncoding\n$global:LASTEXITCODE = 0\n"
        f"& {quote(command_file)}\n"
        "$wright_ok = $?\n$wright_exit = $global:LASTEXITCODE\n"
        f"[IO.File]::WriteAllText({quote(request['cwd_file'])}, (Get-Location).ProviderPath)\n"
        "if (-not $wright_ok -and $wright_exit -eq 0) { $wright_exit = 1 }\nexit $wright_exit\n",
        encoding="utf-8-sig",
    )
    argv = [request["shell"], "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    with open(request["output"], "wb", buffering=0) as output, open("NUL", "rb", buffering=0) as stdin:
        os.set_inheritable(output.fileno(), True)
        os.set_inheritable(stdin.fileno(), True)
        process = native.restricted_process(subprocess.list2cmdline(argv), request["cwd"], request["environment"], request["identities"], request["owners"], msvcrt.get_osfhandle(output.fileno()), msvcrt.get_osfhandle(stdin.fileno()))
    native.close(process.thread)
    try:
        native.wait(process.process, 0xFFFFFFFF)
        code = native.w.DWORD()
        native.check(native.exit_code(process.process, native.c.byref(code)))
        return code.value
    finally:
        native.close(process.process)


if __name__ == "__main__":
    try:
        sys.exit(run(sys.argv[1]))
    except Exception as error:
        Path(sys.argv[1]).with_suffix(".error").write_text(f"{type(error).__name__}: {error}", encoding="utf-8")
        sys.exit(1)
