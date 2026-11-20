"""The system prompt follows the current tools and memory, not the session's creation.

A session stores its first system prompt. When it is resumed with other tools
or with memory turned off, the request must describe what is there now.
"""

from __future__ import annotations

from tests.responses import event, response
from wright.application.composition.agent_factory import create_agent
from wright.application.composition.memory_assembly import (
    assemble_memory_manager,
    memory_tools,
)
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolAccess, ToolResult
from wright.domain.model.tool_definition import Tool


class _Model:
    """Answers every request at once and keeps what was sent."""

    context_limit = 100_000

    def __init__(self):
        self.requests: list[list[dict]] = []

    def __call__(self, messages, *, tools):
        self.requests.append(list(messages))
        yield event(response(content="ok"))


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Does {name}",
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success(),
        access_descriptor=lambda args: ToolAccess.internal_read(),
    )


def _sent_system(llm: _Model) -> str:
    [system] = [message for message in llm.requests[-1] if message.get("role") == "system"]
    return system["content"]


def _stored(session: Session) -> str:
    return next(record.message["content"] for record in session.message_records if record.message.get("role") == "system")


def test_a_new_session_sends_the_prompt_it_stored(tmp_path):
    llm = _Model()
    session = Session.create("fresh", tmp_path)
    create_agent(llm, [_tool("read")], session).run("hi")
    assert _sent_system(llm) == _stored(session)


def test_a_resumed_session_drops_memory_turned_off_since(tmp_path):
    manager = assemble_memory_manager(None, directory=tmp_path / "memory")
    session = Session.create("resumed", tmp_path)
    create_agent(_Model(), [_tool("read"), *memory_tools(manager)], session, memory=manager)
    assert "memory_write" in _stored(session)

    llm = _Model()
    create_agent(llm, [_tool("read")], session, memory=None).run("hi")
    sent = _sent_system(llm)
    assert "memory_write" not in sent
    assert manager.instructions().strip()[:40] not in sent
    # The stored record is history and stays as it was.
    assert "memory_write" in _stored(session)


def test_a_resumed_session_lists_tools_added_since(tmp_path):
    session = Session.create("grown", tmp_path)
    create_agent(_Model(), [_tool("read")], session)
    llm = _Model()
    create_agent(llm, [_tool("read"), _tool("grep")], session).run("hi")
    assert "grep" in _sent_system(llm)
    assert "grep" not in _stored(session)
