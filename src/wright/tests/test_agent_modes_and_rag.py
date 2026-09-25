from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from wright.application.agent_runner import create_agent
from wright.application.runtime import (
    RuntimeConfig,
    assemble_runtime,
    parse_cli_args,
    runtime_config_from_args,
    shutdown_runtime,
)
from wright.domain.model.agent import AgentProfile
from wright.domain.prompt import (
    DEFAULT_CODING_ROLE,
    DEFAULT_GENERAL_ROLE,
    build_system_prompt,
    get_role_instruction,
)
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolResult
from wright.infrastructure.knowledge import optional_knowledge_tools
from wright.infrastructure.tools.base import Tool
from wright.interfaces.renderer import SilentRenderer


def _dummy_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Dummy {name}",
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success(),
    )


class DummyLLM:
    context_limit = 128_000


def test_runtime_config_defaults_and_cli_parsing(monkeypatch):
    default_cfg = RuntimeConfig()
    assert default_cfg.mode == "coding"
    assert default_cfg.with_rag is False

    monkeypatch.setattr(
        sys, "argv", ["wright", "--mode", "general", "--with-rag"]
    )
    args = parse_cli_args()
    assert args.mode == "general"
    assert args.with_rag is True

    cfg = runtime_config_from_args(args)
    assert cfg.mode == "general"
    assert cfg.with_rag is True


def test_role_instruction_presets():
    assert get_role_instruction("coding") == DEFAULT_CODING_ROLE
    assert get_role_instruction("general") == DEFAULT_GENERAL_ROLE
    assert get_role_instruction("other") == DEFAULT_CODING_ROLE


def test_build_system_prompt_role_and_tool_advice():
    # General mode without edit/execute tools
    read_tools = [_dummy_tool("read_file"), _dummy_tool("web_search")]
    prompt = build_system_prompt(
        read_tools,
        role_instruction=DEFAULT_GENERAL_ROLE,
    )
    assert DEFAULT_GENERAL_ROLE in prompt
    assert DEFAULT_CODING_ROLE not in prompt
    assert "Prefer edit_file" not in prompt
    assert "execute_command keeps a working directory" not in prompt

    # Coding mode with edit and execute tools
    coding_tools = [
        _dummy_tool("read_file"),
        _dummy_tool("edit_file"),
        _dummy_tool("write_file"),
        _dummy_tool("execute_command"),
    ]
    coding_prompt = build_system_prompt(
        coding_tools,
        role_instruction=DEFAULT_CODING_ROLE,
    )
    assert DEFAULT_CODING_ROLE in coding_prompt
    assert "Prefer edit_file" in coding_prompt
    assert "execute_command keeps a working directory" in coding_prompt


def test_create_agent_respects_role_instruction(tmp_path):
    session = Session.create("test", tmp_path)
    agent = create_agent(
        DummyLLM(),
        [_dummy_tool("read_file")],
        session,
        SilentRenderer(),
        role_instruction="You are a specialized math solver.",
    )
    system_content = session.message_records[0].message["content"]
    assert "You are a specialized math solver." in system_content
    assert DEFAULT_CODING_ROLE not in system_content


def test_create_agent_respects_profile_role_instruction(tmp_path):
    session = Session.create("test", tmp_path)
    profile = AgentProfile(
        name="researcher",
        allowed_tools=frozenset({"read_file"}),
        role_instruction="You are a research analyst.",
    )
    create_agent(
        DummyLLM(),
        [_dummy_tool("read_file")],
        session,
        SilentRenderer(),
        profile=profile,
    )
    system_content = session.message_records[0].message["content"]
    assert "You are a research analyst." in system_content
    assert DEFAULT_CODING_ROLE not in system_content


def test_optional_knowledge_tools_explicit_switches(monkeypatch):
    # When enabled=False, returns empty even if env is truthy
    monkeypatch.setenv("WRIGHT_KNOWLEDGE_ENABLED", "1")
    assert optional_knowledge_tools(enabled=False) == []

    # When enabled=True, returns tools even if env is unset
    monkeypatch.delenv("WRIGHT_KNOWLEDGE_ENABLED", raising=False)
    tools = optional_knowledge_tools(enabled=True)
    assert len(tools) == 1
    assert tools[0].name == "knowledge_search"


def test_assemble_runtime_mode_and_rag(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("WRIGHT_KNOWLEDGE_ENABLED", raising=False)

    # 1. Default Coding mode without RAG
    cfg_coding = RuntimeConfig(
        workspace=tmp_path,
        model="gpt-4o",
        mode="coding",
        with_rag=False,
    )
    rt_coding = assemble_runtime(cfg_coding, start_automation=False)
    try:
        base_names = {t.name for t in rt_coding.assembled_base_tools}
        assert "edit_file" in base_names
        assert "write_file" in base_names
        assert "execute_command" in base_names
        assert "read_file" in base_names
        assert "knowledge_search" not in base_names

        content = rt_coding.session_state.message_records[0].message["content"]
        assert DEFAULT_CODING_ROLE in content
        assert "Prefer edit_file" in content
    finally:
        shutdown_runtime(rt_coding)

    # 2. General mode with RAG
    workspace_gen = tmp_path / "gen_space"
    workspace_gen.mkdir()
    cfg_general = RuntimeConfig(
        workspace=workspace_gen,
        model="gpt-4o",
        mode="general",
        with_rag=True,
    )
    rt_general = assemble_runtime(cfg_general, start_automation=False)
    try:
        base_names = {t.name for t in rt_general.assembled_base_tools}
        # Coding destructive tools must be absent
        assert "edit_file" not in base_names
        assert "write_file" not in base_names
        assert "execute_command" not in base_names

        # Safe inspection, web, and knowledge tools must be present
        assert "read_file" in base_names
        assert "list_directory" in base_names
        assert "glob" in base_names
        assert "grep" in base_names
        assert "web_search" in base_names
        assert "knowledge_search" in base_names

        content = rt_general.session_state.message_records[0].message["content"]
        assert DEFAULT_GENERAL_ROLE in content
        assert DEFAULT_CODING_ROLE not in content
        assert "Prefer edit_file" not in content
        assert "execute_command keeps a working directory" not in content
    finally:
        shutdown_runtime(rt_general)
