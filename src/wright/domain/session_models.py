"""Shared model-selection policy for every Wright session host.

This is deliberately UI-free: command-line parsing belongs to an entry point,
while the model candidate list is a session configuration concern.
"""

from __future__ import annotations

from os import environ


def process_model_name(cli_model: str | None = None) -> str:
    """Choose a new-session model without changing the established precedence."""
    for candidate in (cli_model, environ.get("OPENAI_MODEL")):
        cleaned = (candidate or "").strip()
        if cleaned:
            return cleaned
    return ""


def available_models(current: str, configured: str | None = None) -> tuple[str, ...]:
    """Return configured candidates, retaining the selected model first."""
    configured = environ.get("WRIGHT_MODELS", "") if configured is None else configured
    models = [model.strip() for model in configured.split(",") if model.strip()]
    if current and current not in models:
        models.insert(0, current)
    return tuple(dict.fromkeys(models))
