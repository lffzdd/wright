from __future__ import annotations

from tests.paths import PACKAGE_ROOT


def test_core_records_context_and_execution_do_not_import_chat_sdk_types():
    root = PACKAGE_ROOT
    for relative in (
        "domain/model/session/session.py", "domain/model/session/run.py", "application/agent/context.py",
        "application/agent/runner.py",
        "application/tool_execution/dispatch.py",
        "infrastructure/tools/executor.py",
        "domain/model/session/conversation.py", "domain/protocol.py", "domain/policy/verifier.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert "openai.types.chat" not in text, relative
        assert "ChatCompletionMessageParam" not in text, relative


def test_session_has_no_live_process_handles_and_web_has_no_tui_business_import():
    root = PACKAGE_ROOT
    session = (root / "domain" / "model" / "session" / "session.py").read_text(encoding="utf-8")
    web_runtime = (root / "interfaces" / "web" / "runtime_manager.py").read_text(encoding="utf-8")

    assert "ProcessRegistry" not in session
    assert "ProcessResources" not in session
    assert "from ..tui" not in web_runtime
