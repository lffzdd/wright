"""Collect permission and ask_user answers with prompt_toolkit.

Display is delegated to ``ConsoleRenderer`` public methods. This class does
not read the renderer's private display state, and it does not route through
an interaction hub. The input controller and ``RoutedPrompter`` do that.
"""

from __future__ import annotations

from typing import Any

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML

from ...domain.policy.permission.types import PermissionPrompt
from .console_renderer import ConsoleRenderer


class ConsolePrompter:
    """Terminal implementation of the application ``UserPrompter`` contract."""

    def __init__(self, renderer: ConsoleRenderer) -> None:
        self._renderer = renderer

    def prompt_permission(self, permission_prompt: PermissionPrompt) -> str:
        return self.collect_permission(permission_prompt.to_dict())

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None:
        return self.collect_user({
            "question": question,
            "context": context,
            "options": options,
        })

    def collect_permission(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or ()
        self._renderer.present_permission(
            tool_name=str(payload.get("tool_name", "")),
            subject=str(payload.get("subject", "")),
            risk_flags=payload.get("risk_flags") or (),
            reason=str(payload.get("reason", "")),
            targets=payload.get("targets") or (),
            choices=choices,
            principal=str(payload.get("principal", "")),
        )
        prompt_text = HTML("  <b><ansiyellow>允许执行? </ansiyellow></b>")
        try:
            answer = prompt(prompt_text).strip().lower()
            choice_ids = {str(choice["id"]) for choice in choices}
            if answer == "y":
                answer = "allow_once"
            elif answer == "n" or not answer:
                answer = "deny"
            elif answer == "a":
                answer = next(
                    (
                        choice_id
                        for choice_id in (
                            "allow_session_directory",
                            "allow_persistent_directory",
                        )
                        if choice_id in choice_ids
                    ),
                    "deny",
                )
            return answer if answer in choice_ids else "deny"
        except (EOFError, KeyboardInterrupt):
            return "deny"
        finally:
            self._renderer.resume_display()

    def collect_user(self, payload: dict[str, Any]) -> str | None:
        options = tuple(payload.get("options") or ())
        self._renderer.present_question(
            question=str(payload.get("question", "")),
            context=str(payload.get("context", "")),
            options=options,
        )
        prompt_text = HTML("<b><ansicyan>你的回答 ❯ </ansicyan></b>")
        while True:
            try:
                answer = prompt(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                self._renderer.show_blank()
                self._renderer.resume_display()
                return None
            if answer:
                self._renderer.resume_display()
                return answer
            self._renderer.show_line("回答不能为空，请重新输入。", style="yellow")
