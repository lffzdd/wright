"""Modal screens for permission, questions, history resume, and model choice."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class PermissionModal(ModalScreen[str]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "deny", "Deny", show=False),
    ]

    def __init__(
        self,
        tool_name: str,
        subject: str,
        risk_flags: list[str] | tuple[str, ...],
        reason: str,
        targets: list[str] | tuple[str, ...] = (),
        choices: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        principal: str = "",
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.subject = subject
        self.risk_flags = tuple(risk_flags)
        self.reason = reason
        self.targets = tuple(targets)
        self.choices = tuple(choices)
        self.principal = principal

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("permission", classes="dialog-kicker")
            yield Static(self.tool_name, classes="dialog-title")
            if self.subject:
                yield Static(self.subject, classes="dialog-subject")
            yield Static(f"risk  {', '.join(self.risk_flags)}", classes="dialog-meta")
            yield Static(self.reason, classes="dialog-reason")
            if self.targets:
                yield Static("targets  " + "; ".join(self.targets), classes="dialog-meta")
            if self.principal:
                yield Static(f"principal  {self.principal}", classes="dialog-meta")
            with Horizontal(classes="dialog-actions"):
                for choice in self.choices:
                    variant = "error" if choice.get("id") == "deny" else "primary"
                    yield Button(
                        f"{choice.get('label', choice.get('id', 'choice'))}",
                        id=f"choice-{choice.get('id', 'deny')}",
                        variant=variant,
                    )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if ident.startswith("choice-"):
            self.dismiss(ident.removeprefix("choice-") or "deny")

    def action_deny(self) -> None:
        self.dismiss("deny")

class AskUserModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.question = question
        self.context = context
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("question", classes="dialog-kicker")
            yield Static(self.question, classes="dialog-title")
            if self.context:
                yield Static(self.context, classes="dialog-reason")
            if self.options:
                with Vertical(classes="dialog-options"):
                    for idx, option in enumerate(self.options, start=1):
                        yield Button(f"{idx}. {option}", id=f"opt-{idx}", variant="primary")
            yield Input(placeholder="type an answer", id="answer")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if ident.startswith("opt-"):
            try:
                idx = int(ident.split("-", 1)[1]) - 1
            except ValueError:
                return
            if 0 <= idx < len(self.options):
                self.dismiss(self.options[idx])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

class ResumeModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("resume", classes="dialog-kicker")
            yield Static("Saved sessions", classes="dialog-title")
            with Vertical(classes="dialog-options"):
                for index, session in enumerate(self.sessions):
                    goal = str(session.get("user_goal") or "(no goal)").replace("\n", " ")
                    label = f"{session['session_id']}  ·  {goal[:52]}"
                    yield Button(label, id=f"session-{index}", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if not ident.startswith("session-"):
            return
        try:
            session = self.sessions[int(ident.removeprefix("session-"))]
        except (ValueError, IndexError):
            return
        self.dismiss(str(session["session_id"]))

    def action_cancel(self) -> None:
        self.dismiss(None)

class ModelModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, models: tuple[str, ...], current: str) -> None:
        super().__init__()
        self.models = models
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("model", classes="dialog-kicker")
            yield Static(f"Current: {self.current}", classes="dialog-title")
            if self.models:
                with Vertical(classes="dialog-options"):
                    for index, model in enumerate(self.models):
                        yield Button(model, id=f"model-{index}", variant="primary")
            yield Input(placeholder="type a model ID", id="model-input")

    def on_mount(self) -> None:
        self.query_one("#model-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        ident = event.button.id or ""
        if not ident.startswith("model-"):
            return
        try:
            self.dismiss(self.models[int(ident.removeprefix("model-"))])
        except (ValueError, IndexError):
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)
