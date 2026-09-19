from __future__ import annotations

from pathlib import Path


def test_core_records_context_and_execution_do_not_import_chat_sdk_types():
    root = Path(__file__).parents[1]
    for relative in (
        "domain/session.py", "domain/runs.py", "engine/context.py",
        "engine/agent.py", "engine/executor.py", "domain/checkpoint.py",
        "domain/conversation.py", "util.py", "engine/verifier.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert "openai.types.chat" not in text, relative
        assert "ChatCompletionMessageParam" not in text, relative


def test_session_has_no_live_process_handles_and_web_has_no_tui_business_import():
    root = Path(__file__).parents[1]
    session = (root / "domain" / "session.py").read_text(encoding="utf-8")
    web_runtime = (root / "web" / "runtime_manager.py").read_text(encoding="utf-8")

    assert "ProcessRegistry" not in session
    assert "ProcessResources" not in session
    assert "from ..tui" not in web_runtime
