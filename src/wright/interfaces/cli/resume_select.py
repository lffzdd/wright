"""Ask which checkpoint to resume. Callers that cannot prompt must pass an id."""

from __future__ import annotations

from prompt_toolkit import prompt

from ...infrastructure.persistence.session.errors import CheckpointError
from ..i18n import t


def choose_resume_session(recent: list[dict]) -> str:
    """Print a numbered history menu and return the chosen session id."""
    if not recent:
        raise CheckpointError(t("cli.no_checkpoints"))
    print("\n" + t("cli.choose_session"))
    for index, item in enumerate(recent, 1):
        goal = item["user_goal"] or t("cli.no_goal")
        if len(goal) > 40:
            goal = goal[:37] + "..."
        print(
            f"  [{index}] {item['saved_at']} ({item['session_id']}) "
            f'| "{goal}" (status: {item["status"]})'
        )
    print()
    choice = prompt(t("cli.choice_prompt")).strip()
    selected = 0
    if choice:
        try:
            selected = int(choice) - 1
        except ValueError:
            raise CheckpointError(t("cli.choice_invalid", choice=choice)) from None
    if not 0 <= selected < len(recent):
        raise CheckpointError(t("cli.choice_range", choice=choice))
    return recent[selected]["session_id"]
