"""Session-control requests and model choices for the fullscreen TUI."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from os import environ


@dataclass(frozen=True)
class SessionControlRequest:
    """A safe runtime transition requested by the TUI."""

    kind: str
    session_id: str | None = None

    @classmethod
    def new(cls) -> SessionControlRequest:
        return cls("new")

    @classmethod
    def resume(cls, session_id: str) -> SessionControlRequest:
        return cls("resume", session_id)


def runtime_args_for_transition(
    args: Namespace,
    request: SessionControlRequest,
) -> Namespace:
    """Copy CLI options and replace only their session-selection fields."""
    values = vars(args).copy()
    values["continue_latest"] = False
    values["resume"] = request.session_id if request.kind == "resume" else None
    return Namespace(**values)


def available_models(current: str, configured: str | None = None) -> tuple[str, ...]:
    """Read optional WRIGHT_MODELS while always retaining the active model."""
    configured = environ.get("WRIGHT_MODELS", "") if configured is None else configured
    models = [model.strip() for model in configured.split(",") if model.strip()]
    if current and current not in models:
        models.insert(0, current)
    return tuple(dict.fromkeys(models))
