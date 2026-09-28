"""Adapt a permission approval request to the injected user prompter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...domain.policy.permission.approval import PermissionRequest
from ...domain.policy.permission.types import PermissionResponse
from ..session.interaction import UserPrompter

PhaseNotifier = Callable[[Any, str], None]


class InteractiveApprovalHandler:
    """Ask the injected ``UserPrompter`` for a choice ID.

    The resolver commits any resulting authorization. This adapter only
    collects the choice. ``notify_phase`` tells the display that approval is
    pending; it is not how the question is asked. Hub-versus-fallback routing
    belongs to ``RoutedPrompter``, not to this adapter.
    """

    def __init__(
        self,
        prompter: UserPrompter,
        *,
        notify_phase: PhaseNotifier | None = None,
    ):
        self._prompter = prompter
        self._notify_phase = notify_phase

    def __call__(self, request: PermissionRequest) -> PermissionResponse:
        try:
            if self._notify_phase is not None:
                self._notify_phase(request.tool_call, "awaiting_approval")
            response = self._prompter.prompt_permission(request.prompt)
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
