"""Failed and cancelled turns stay visible where they happened."""

from wright.application.workspace.timeline import project_timeline
from wright.domain.model.session import Session


def _turn(session, prompt, answer=None):
    session.begin_user_turn(prompt)
    session.append_user_message(prompt)
    if answer is not None:
        session.append_message({"role": "assistant", "content": answer}, source="model")


def test_outcomes_are_placed_after_their_own_turn(tmp_path):
    session = Session.create("goal", tmp_path)
    _turn(session, "first")
    session.mark_failed(error="Model request failed (HTTP 400): bad")
    _turn(session, "second")
    session.mark_cancelled()
    _turn(session, "third", answer="done")
    session.mark_completed()

    kinds = [(item["kind"], item.get("role") or item.get("status"), item.get("text", "")) for item in project_timeline(session)]
    assert kinds == [
        ("text", "user", "first"),
        ("status", "failed", "Model request failed (HTTP 400): bad"),
        ("text", "user", "second"),
        ("status", "cancelled", ""),
        ("text", "user", "third"),
        ("text", "assistant", "done"),
    ]
