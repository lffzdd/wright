"""Renderer-backed structured permission approval."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .resolver import PermissionRequest
from .types import PermissionResponse

if TYPE_CHECKING:
    from ..renderer import Renderer


class InteractiveApprovalHandler:
    """Collect a choice ID; persistence is committed by the resolver."""

    def __init__(self, renderer: Renderer):
        self._renderer = renderer

    def __call__(self, request: PermissionRequest) -> PermissionResponse:
        try:
            self._renderer.on_tool_phase(request.tool_call, "awaiting_approval")
            response = self._renderer.prompt_permission(request.prompt)
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
