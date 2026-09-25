"""Adapt a permission approval request to the active user interface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..permission.approval import PermissionRequest
from ..permission.types import PermissionPrompt, PermissionResponse

PhaseNotifier = Callable[[Any, str], None]
PromptFallback = Callable[[PermissionPrompt], str | PermissionResponse]


class InteractiveApprovalHandler:
    """Ask the interaction hub for a choice ID.

    The resolver commits any resulting authorization. This adapter only
    collects the choice. ``notify_phase`` tells the display that approval is
    pending; it is not how the question is asked. When no hub is bound, or the
    caller is already the thread collecting answers, ``prompt_fallback`` asks
    the interface directly so that thread does not wait on itself.
    """

    def __init__(
        self,
        interaction: Any,
        *,
        notify_phase: PhaseNotifier | None = None,
        prompt_fallback: PromptFallback | None = None,
    ):
        self._interaction = interaction
        self._notify_phase = notify_phase
        self._prompt_fallback = prompt_fallback

    def __call__(self, request: PermissionRequest) -> PermissionResponse:
        try:
            if self._notify_phase is not None:
                self._notify_phase(request.tool_call, "awaiting_approval")
            response = self._ask(request)
        except Exception:
            return PermissionResponse("deny")
        if isinstance(response, PermissionResponse):
            choice = response.choice
            updated = response.updated_arguments
        else:
            choice = str(response or "")
            updated = None
        valid = {choice_item.id for choice_item in request.prompt.choices}
        if choice not in valid:
            return PermissionResponse("deny")
        return PermissionResponse(choice, updated)

    def _ask(self, request: PermissionRequest) -> Any:
        interaction = self._interaction
        on_collector = getattr(interaction, "is_collector_thread", None)
        collecting = bool(on_collector()) if callable(on_collector) else False
        if interaction is not None and not collecting:
            return interaction.request("permission", request.prompt.to_dict())
        if self._prompt_fallback is None:
            return "deny"
        return self._prompt_fallback(request.prompt)
