"""Pure slash-command completion state for the Textual composer."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..session_host import SlashCommand, slash_command_matches

TUI_SLASH_COMMANDS = (
    SlashCommand("/new", "start a new session", "/new"),
    SlashCommand("/resume", "open a saved session", "/resume"),
    SlashCommand("/model", "change the active model", "/model"),
    SlashCommand("/clear", "clear the visible transcript", "/clear"),
    SlashCommand("/exit", "close Wright", "/exit"),
    SlashCommand("/quit", "close Wright", "/quit"),
)


def tui_command_catalog() -> tuple[SlashCommand, ...]:
    """Return every command visible in the fullscreen host."""
    return slash_command_matches("/", TUI_SLASH_COMMANDS)


def tui_help_text() -> str:
    lines = ["Commands:"]
    for command in tui_command_catalog():
        lines.append(f"  {command.usage:<28} {command.description}")
    return "\n".join(lines)


@dataclass
class SlashCompletion:
    """Tracks matches and selection without depending on Textual widgets."""

    extra_commands: tuple[SlashCommand, ...] = TUI_SLASH_COMMANDS
    matches: tuple[SlashCommand, ...] = field(default_factory=tuple)
    selected_index: int = 0

    def update(self, text: str) -> None:
        self.matches = slash_command_matches(text, self.extra_commands)
        self.selected_index = min(self.selected_index, max(0, len(self.matches) - 1))

    def move(self, offset: int) -> None:
        if self.matches:
            self.selected_index = (self.selected_index + offset) % len(self.matches)

    @property
    def selected(self) -> SlashCommand | None:
        if not self.matches:
            return None
        return self.matches[self.selected_index]
