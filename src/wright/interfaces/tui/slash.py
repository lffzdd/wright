"""Pure slash-command completion state for the Textual composer."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...application.session.dispatch import (
    SlashCommand,
    slash_command_matches,
)
from ..i18n import t


def tui_slash_commands() -> tuple[SlashCommand, ...]:
    """Commands for the fullscreen host, in the active interface language."""
    return (
        SlashCommand("/new", t("tui.command.new"), "/new [local|worktree]"),
        SlashCommand("/resume", t("tui.command.resume"), "/resume [session_id]"),
        SlashCommand("/close", t("tui.command.close"), "/close"),
        SlashCommand("/stop", t("tui.command.stop"), "/stop"),
        SlashCommand("/stop-all", t("tui.command.stop_all"), "/stop-all"),
        SlashCommand("/cancel", t("tui.command.cancel"), "/cancel COMMAND_ID"),
        SlashCommand("/status", t("tui.command.status"), "/status"),
        SlashCommand("/model", t("tui.command.model"), "/model [name]"),
        SlashCommand("/mode", t("tui.command.mode"), "/mode agent|plan|ask"),
        SlashCommand("/permission", t("tui.command.permission"), "/permission default|acceptEdits|plan|bypass"),
        SlashCommand("/ref", t("tui.command.ref"), "/ref PATH"),
        SlashCommand("/doc", t("tui.command.doc"), "/doc PATH"),
        SlashCommand("/env", t("tui.command.env"), "/env"),
        SlashCommand("/language", t("tui.command.language"), "/language [en|zh-CN]"),
        SlashCommand("/clear", t("tui.command.clear"), "/clear"),
        SlashCommand("/exit", t("tui.command.exit"), "/exit"),
        SlashCommand("/quit", t("tui.command.exit"), "/quit"),
    )


def tui_command_catalog() -> tuple[SlashCommand, ...]:
    """Return every command visible in the fullscreen host."""
    return slash_command_matches("/", tui_slash_commands())


def tui_help_text() -> str:
    lines = [t("tui.help.header") + ":"]
    for command in tui_command_catalog():
        lines.append(f"  {command.usage:<28} {command.description}")
    return "\n".join(lines)


@dataclass
class SlashCompletion:
    """Tracks matches and selection without depending on Textual widgets."""

    extra_commands: tuple[SlashCommand, ...] | None = None
    matches: tuple[SlashCommand, ...] = field(default_factory=tuple)
    selected_index: int = 0

    def update(self, text: str) -> None:
        commands = self.extra_commands if self.extra_commands is not None else tui_slash_commands()
        self.matches = slash_command_matches(text, commands)
        self.selected_index = min(self.selected_index, max(0, len(self.matches) - 1))

    def move(self, offset: int) -> None:
        if self.matches:
            self.selected_index = (self.selected_index + offset) % len(self.matches)

    @property
    def selected(self) -> SlashCommand | None:
        if not self.matches:
            return None
        return self.matches[self.selected_index]
