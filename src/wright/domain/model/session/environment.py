"""Where a session's files are executed, independent of workspace discovery."""

from __future__ import annotations

from typing import Literal

ExecutionEnvironment = Literal["local", "worktree"]
