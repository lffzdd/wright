from jsonschema import validators

from wright.application.composition.roles import tools_for_role
from wright.application.skills import SkillRegistry
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.knowledge import MAX_HIT_CONTENT_CHARS, KnowledgeHit
from wright.domain.model.tool import ToolCall
from wright.infrastructure.storage.skills import write_skill
from wright.infrastructure.tools.knowledge import (
    build_knowledge_tools,
    optional_knowledge_tools,
)
from wright.infrastructure.tools.skill_tools import build_skill_tools


class FakeProvider:
    def __init__(self, hits=None, error=None):
        self.hits = hits or []
        self.error = error
        self.queries = []

    def search(self, query, top_k):
        self.queries.append((query, top_k))
        if self.error is not None:
            raise self.error
        return list(self.hits)


def test_top_k_out_of_range_rejected_by_schema(tmp_path):
    tool = build_knowledge_tools(FakeProvider())[0]
    outcome = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
    ).execute([
        ToolCall("knowledge_search", {"query": "q", "top_k": 11}, "c1")
    ])[0]
    assert outcome.status == "failed"
    assert outcome.result.data["error"]["type"] == "tool_input_validation"
    assert any(
        issue["validator"] in {"maximum", "minimum"}
        for issue in outcome.result.data["error"]["issues"]
    )


def test_schema_is_valid_json_schema():
    tool = build_knowledge_tools(FakeProvider())[0]
    validator_cls = validators.validator_for(tool.parameters)
    validator_cls.check_schema(tool.parameters)


def test_access_descriptor_declares_network_access():
    tool = build_knowledge_tools(FakeProvider())[0]
    result = tool.describe_access({"query": "q"})
    assert result.operations == frozenset({"network_read"})
    assert "network_read" in result.risk_flags


def test_tool_wraps_untrusted_content_and_truncates():
    provider = FakeProvider([
        KnowledgeHit(content="密" * (MAX_HIT_CONTENT_CHARS + 50), score=0.9, source="doc.md"),
    ])
    result = build_knowledge_tools(provider)[0].call({"query": "q", "top_k": 1}, None)
    assert result.ok
    assert result.data["warning"]
    assert result.data["truncated"] is True
    content = result.data["hits"][0]["content"]
    assert "<untrusted-knowledge source=\"doc.md\">" in content
    assert "not verified fact" in result.data["warning"]


def test_knowledge_tools_absent_when_disabled(monkeypatch):
    monkeypatch.delenv("WRIGHT_KNOWLEDGE_ENABLED", raising=False)
    assert optional_knowledge_tools() == []


def test_knowledge_tools_present_when_enabled(monkeypatch):
    monkeypatch.setenv("WRIGHT_KNOWLEDGE_ENABLED", "1")
    names = [tool.name for tool in optional_knowledge_tools()]
    assert names == ["knowledge_search"]


def test_child_keeps_knowledge_search_but_drops_skill_tools(tmp_path):
    write_skill(
        tmp_path,
        "release-check",
        name="release-check",
        description="发布时使用",
        body="先跑测试",
    )
    tools = tools_for_role([
        *build_knowledge_tools(FakeProvider()),
        *build_skill_tools(SkillRegistry(tmp_path)),
    ], "child")
    names = {tool.name for tool in tools}
    assert "knowledge_search" in names
    assert "skill" not in names
    assert "list_skills" not in names
    assert "load_skill" not in names
    assert "unload_skill" not in names
