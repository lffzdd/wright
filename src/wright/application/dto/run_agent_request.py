"""Data Transfer Object for initiating an agent execution run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunAgentRequest:
    """Request payload to initiate or resume an agent run."""

    prompt: str
    session_id: str = ""
    model: str | None = None
    workspace_dir: Path | None = None
    max_steps: int | None = None
    environment: str = "local"
    system_prompt: str | None = None
    extra_tools: tuple[str, ...] = ()
    run_config: dict[str, Any] = field(default_factory=dict)
