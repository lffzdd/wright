"""Interactive prompt and approval handling for user confirmation."""

from __future__ import annotations

from ..interaction import (
    PROMPT_INTERRUPTED,
    InteractionBroker,
    InteractionHub,
    InteractionKind,
    InteractionRequest,
    InteractionResponse,
)

# Canonical InteractivePrompter represents the CLI user prompter / confirmation handler
InteractivePrompter = InteractionBroker

__all__ = [
    "InteractionBroker",
    "InteractionHub",
    "InteractionKind",
    "InteractionRequest",
    "InteractionResponse",
    "InteractivePrompter",
    "PROMPT_INTERRUPTED",
]
