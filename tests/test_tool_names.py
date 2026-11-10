"""Legacy built-in tool names keep working at every input boundary."""

import json

from tests.test_checkpoint import _populated_session
from wright.domain.model.lifecycle import HookRegistration
from wright.domain.model.tool_definition import Tool
from wright.domain.model.tool_names import LEGACY_TOOL_NAMES, canonical_tool_name
from wright.domain.protocol import encode_tools
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.storage.skills import _allowed_tools
from wright.infrastructure.tools import execute_command_tool
from wright.infrastructure.tools.file import file_tools


def _tool(name: str) -> Tool:
    return Tool(name=name, description=name, parameters={}, call=lambda args, runtime: None)


def test_builtin_tools_use_the_canonical_short_names():
    names = {tool.name for tool in file_tools} | {execute_command_tool.name}
    assert {"read", "write", "edit", "ls", "shell", "grep", "glob"} <= names
    assert not names & set(LEGACY_TOOL_NAMES)


def test_canonical_names_map_legacy_names_and_pass_others_through():
    assert canonical_tool_name("execute_command") == "shell"
    assert canonical_tool_name("list_directory") == "ls"
    assert canonical_tool_name("shell") == "shell"
    assert canonical_tool_name("mcp__x__read_file") == "mcp__x__read_file"


def test_model_may_repeat_a_legacy_name_but_only_new_names_are_advertised():
    schemas, names = encode_tools([_tool("shell"), _tool("read")])

    assert [schema["name"] for schema in schemas] == ["shell", "read"]
    assert names["execute_command"] == "shell"
    assert names["read_file"] == "read"
    # A legacy alias exists only for a tool exposed this turn.
    assert "edit_file" not in names


def test_hook_matchers_written_for_legacy_names_keep_firing():
    def hook(event):
        return None

    exact = HookRegistration("pre_tool_use", hook, matcher="execute_command")
    pattern = HookRegistration("pre_tool_use", hook, matcher="*_file")
    current = HookRegistration("pre_tool_use", hook, matcher="shell")

    assert exact.matches({"tool_name": "shell"})
    assert pattern.matches({"tool_name": "edit"})
    assert current.matches({"tool_name": "shell"})
    assert not exact.matches({"tool_name": "read"})
    assert not pattern.matches({"tool_name": "shell"})


def test_skill_allowed_tools_are_canonicalized():
    assert _allowed_tools("read_file execute_command grep", field="allowed-tools") == (
        "read", "shell", "grep",
    )


def test_checkpoint_with_legacy_tool_names_loads_under_new_names(tmp_path):
    session = _populated_session(tmp_path)
    store = FileSessionRepository(tmp_path / "checkpoints")
    path = store.save(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    execution = payload["session"]["tool_executions"]["call_1"]
    assert execution["call"]["name"] == "write"
    execution["call"]["name"] = "write_file"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = store.load(session.session_id)

    assert restored.tool_executions["call_1"].call.name == "write"
