"""Shell wrappers that keep an authorized command out of the wrapper text.

The command still runs as shell, including pipes and ``cd``. It is written to
its own file so quotes and other syntax cannot rewrite the exit-code or
working-directory trailer that follows it.
"""

from __future__ import annotations

import shlex
from pathlib import Path


def bash_launch_script(command: str, scratch: Path, cwd_file: Path) -> str:
    body = scratch / "command.sh"
    body.write_text(command if command.endswith("\n") else f"{command}\n", encoding="utf-8")
    return (
        f"source {shlex.quote(str(body))}\n"
        "wright_exit=$?\n"
        f"pwd -P > {shlex.quote(str(cwd_file))}\n"
        "exit $wright_exit\n"
    )


def powershell_launch_script(command: str, scratch: Path, cwd_file: Path) -> str:
    body = scratch / "command.ps1"
    body.write_text(command if command.endswith("\n") else f"{command}\n", encoding="utf-8-sig")

    def quote(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    return (
        f"& {quote(body)}\n"
        "$wrightCode=if ($?) {0} else {1}; if ($null -ne $LASTEXITCODE) {$wrightCode=$LASTEXITCODE}\n"
        f"[IO.File]::WriteAllText({quote(cwd_file)}, (Get-Location).Path, [Text.UTF8Encoding]::new($false))\n"
        "exit $wrightCode\n"
    )
