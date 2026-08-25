import pytest

from ...session import SessionState


def test_session_does_not_have_waiting_user_status(tmp_path):
    """确认 waiting_user 状态已从 SessionStatus 中移除。"""
    session = SessionState.create("goal", tmp_path)
    assert session.status == "running"

    # waiting_user 不再是合法状态值
    valid_statuses = {"running", "completed", "failed", "max_steps"}
    session.mark_completed()
    assert session.status in valid_statuses
    session.mark_failed()
    assert session.status in valid_statuses
    session.mark_max_steps()
    assert session.status in valid_statuses
    session.mark_running()
    assert session.status in valid_statuses


def test_session_no_longer_has_question_lifecycle_methods(tmp_path):
    """确认旧的状态机方法已被移除。"""
    session = SessionState.create("goal", tmp_path)

    assert not hasattr(session, "request_user_question")
    assert not hasattr(session, "answer_user_question")
    assert not hasattr(session, "set_pending_question_budget")
    assert not hasattr(session, "pending_user_question")
    assert not hasattr(session, "pending_user_question_id")
