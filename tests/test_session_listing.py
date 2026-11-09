"""The recent-session list shows real checkpoints with recognizable titles."""

import json

from wright.domain.model.session import Session
from wright.infrastructure.persistence.session.repository import FileSessionRepository


def test_listing_skips_bookkeeping_and_titles_by_request(tmp_path):
    store = FileSessionRepository(tmp_path / "sessions")
    session = Session.create("(interactive session)", tmp_path)
    session.begin_user_turn("fix the login bug")
    session.append_user_message("fix the login bug")
    session.mark_failed(error="Model request failed (HTTP 400): bad")
    store.save(session)
    (tmp_path / "sessions" / "open_requests.json").write_text(json.dumps({"abc": "def"}))

    rows = store.list_recent_sessions(limit=10)

    assert [row["session_id"] for row in rows] == [session.session_id]
    assert rows[0]["user_goal"] == "fix the login bug"
    assert rows[0]["status"] == "failed"
    assert store.latest_session_id() == session.session_id


def test_title_stays_on_first_request_after_follow_ups(tmp_path):
    store = FileSessionRepository(tmp_path / "sessions")
    session = Session.create("(interactive session)", tmp_path)
    for prompt in ("list web/src", "ok", "?"):
        session.begin_user_turn(prompt)
        session.append_user_message(prompt)
        session.mark_completed()

    assert session.display_title() == "list web/src"
    # The current-turn goal keeps following the latest request.
    assert session.current_goal() == "?"
    store.save(session)
    assert store.list_recent_sessions(limit=10)[0]["user_goal"] == "list web/src"


def test_label_overrides_first_request(tmp_path):
    session = Session.create("(interactive session)", tmp_path)
    session.begin_user_turn("list web/src")
    session.session_label = "Web audit"
    assert session.display_title() == "Web audit"
