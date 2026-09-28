"""Ask which checkpoint to resume. Callers that cannot prompt must pass an id."""

from __future__ import annotations

from prompt_toolkit import prompt

from ...infrastructure.persistence.session.errors import CheckpointError


def choose_resume_session(recent: list[dict]) -> str:
    """Print a numbered history menu and return the chosen session id."""
    if not recent:
        raise CheckpointError("没有找到任何可恢复的历史 checkpoint")
    print("\n请选择要恢复的历史会话：")
    for index, item in enumerate(recent, 1):
        goal = item["user_goal"] or "(无目标描述)"
        if len(goal) > 40:
            goal = goal[:37] + "..."
        print(
            f"  [{index}] {item['saved_at']} ({item['session_id']}) "
            f'| "{goal}" (status: {item["status"]})'
        )
    print()
    choice = prompt("输入序号 (默认 [1]): ").strip()
    selected = 0
    if choice:
        try:
            selected = int(choice) - 1
        except ValueError:
            raise CheckpointError(f"无效的选择: {choice}") from None
    if not 0 <= selected < len(recent):
        raise CheckpointError(f"选择超出范围: {choice}")
    return recent[selected]["session_id"]
