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
        operation: str = "",
        grant_summary: str = "",
        preview: str = "",
        cwd: str = "",
        command: str = "",
        http_method: str = "",
        http_target: str = "",
        shell_note: str = "",
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.subject = subject
        self.risk_flags = tuple(risk_flags)
        self.reason = reason
        self.targets = tuple(targets)
        self.choices = tuple(choices)
        self.principal = principal
        self.operation = operation
        self.grant_summary = grant_summary
        self.preview = preview
        self.cwd = cwd
        self.command = command
        self.http_method = http_method
        self.http_target = http_target
        self.shell_note = shell_note

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("permission", classes="dialog-kicker")
            yield Static(self.tool_name, classes="dialog-title", markup=False)
            if self.operation:
                yield Static(f"operation  {self.operation}", classes="dialog-meta", markup=False)
            if self.subject:
                yield Static(self.subject, classes="dialog-subject", markup=False)
            yield Static(f"risk  {', '.join(self.risk_flags)}", classes="dialog-meta", markup=False)
            yield Static(self.reason, classes="dialog-reason", markup=False)
            if self.grant_summary:
                yield Static(self.grant_summary, classes="dialog-reason", markup=False)
            if self.command:
                yield Static(f"command  {self.command}", classes="dialog-meta", markup=False)
            if self.cwd:
                yield Static(f"cwd  {self.cwd}", classes="dialog-meta", markup=False)
            if self.shell_note:
                yield Static(self.shell_note, classes="dialog-reason", markup=False)
            if self.http_method or self.http_target:
                yield Static(
                    f"http  {self.http_method} {self.http_target}".strip(),
                    classes="dialog-meta",
                    markup=False,
                )
            if self.preview:
                yield Static(self.preview, classes="dialog-reason", markup=False)
            if self.targets:
                yield Static("targets  " + "; ".join(self.targets), classes="dialog-meta", markup=False)
            if self.principal:
                yield Static(f"principal  {self.principal}", classes="dialog-meta", markup=False)
            with Horizontal(classes="dialog-actions"):
                for choice in self.choices:
                    variant = "error" if choice.get("id") == "deny" else "primary"
                    label = choice.get("label", choice.get("id", "choice"))
                    scope = choice.get("scope", "")
                    persistence = choice.get("persistence", "")
                    yield Button(
                        f"{label} — {scope} — {persistence}",
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
