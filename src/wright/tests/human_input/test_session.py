
from ...session import Session


def test_session_lifecycle_is_independent_of_run_status(tmp_path):
    """A Session stays open while each Run carries its own execution state."""
    session = Session.create("goal", tmp_path)
    assert session.lifecycle == "open"
    assert session.current_run_status() == "idle"
    session.begin_user_turn("goal")
    assert session.current_run_status() == "running"
    session.mark_completed()
    assert session.current_run_status() == "completed"
    assert session.lifecycle == "open"


def test_session_no_longer_has_question_lifecycle_methods(tmp_path):
    """确认旧的状态机方法已被移除。"""
    session = Session.create("goal", tmp_path)

    assert not hasattr(session, "request_user_question")
    assert not hasattr(session, "answer_user_question")
    assert not hasattr(session, "set_pending_question_budget")
    assert not hasattr(session, "pending_user_question")
    assert not hasattr(session, "pending_user_question_id")
