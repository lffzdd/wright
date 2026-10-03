"""User-question contract used by permission checks and ask_user.

Hosts implement ``UserPrompter``. ``RoutedPrompter`` is the one application
router: an agent thread waits on the interaction hub, and the collector
thread asks the fallback prompter directly so it cannot deadlock on itself.
Output events stay on ``SessionEvents`` and are injected separately.
"""

from __future__ import annotations

from typing import Any, Protocol

from ...domain.policy.permission.types import PermissionPrompt, PermissionResponse


class UserPrompter(Protocol):
    """Collect a permission choice or an ask_user answer."""

    def prompt_permission(
        self, permission_prompt: PermissionPrompt
    ) -> str | PermissionResponse: ...

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None: ...


class DeniedPrompter:
    """Fail closed when the host cannot collect an answer."""

    def prompt_permission(
        self, permission_prompt: PermissionPrompt
    ) -> str:
        del permission_prompt
        return "deny"

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> None:
        del question, context, options
        return None


class RoutedPrompter:
    """``UserPrompter`` that waits on a hub unless already on its collector."""

    def __init__(
        self,
        interaction: Any = None,
        *,
        fallback: UserPrompter | None = None,
    ) -> None:
        self._interaction = interaction
        self._fallback: UserPrompter = fallback or DeniedPrompter()

    def _on_collector_thread(self) -> bool:
        check = getattr(self._interaction, "is_collector_thread", None)
        return bool(check()) if callable(check) else False

    def prompt_permission(
        self, permission_prompt: PermissionPrompt
    ) -> str | PermissionResponse:
        return self.prompt_permission_guarded(permission_prompt)

    def prompt_permission_guarded(self, permission_prompt: PermissionPrompt, validator=None):
        payload = permission_prompt.to_dict()
        interaction = self._interaction
        if validator is not None:
            validator()
        if interaction is not None and not self._on_collector_thread():
            kwargs = {"validator": validator} if validator is not None else {}
            answer = interaction.request("permission", payload, **kwargs)
            return answer if isinstance(answer, PermissionResponse) else str(answer)
        answer = self._fallback.prompt_permission(permission_prompt)
        if validator is not None:
            validator()
        return answer

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None:
        payload = {"question": question, "context": context, "options": list(options)}
        interaction = self._interaction
        if interaction is not None and not self._on_collector_thread():
            answer = interaction.request("ask_user", payload)
            return str(answer) if answer is not None else None
        return self._fallback.prompt_user(question, context, options)
