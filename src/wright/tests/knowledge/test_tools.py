from jsonschema import validators

from ...executor import ToolExecutor
from ...knowledge import optional_knowledge_tools
from ...knowledge.provider import KnowledgeHit, MAX_HIT_CONTENT_CHARS
from ...subagent import _child_base_tools
from ...tools.base import ToolCall, ToolRuntime
from ...tools.knowledge_tools import build_knowledge_tools
from ...tools.skill_tools import build_skill_tools
from ...skills.registry import SkillRegistry
from ...skills.store import write_skill


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
    outcome = ToolExecutor(
        {tool.name: tool}, workspace_dir=tmp_path
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


def test_check_permission_declares_network_access():
    tool = build_knowledge_tools(FakeProvider())[0]
    runtime = ToolRuntime(tool_name="knowledge_search")
    result = tool.check_permission({"query": "q"}, runtime)
    assert result.decision == "ask"
    assert "accesses_network" in result.risk_flags


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
    assert "不能当作已核实事实" in result.data["warning"]


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
        name="发布前检查",
        description="发布时使用",
        body="先跑测试",
    )
    tools = _child_base_tools([
        *build_knowledge_tools(FakeProvider()),
        *build_skill_tools(SkillRegistry(tmp_path)),
    ])
    names = {tool.name for tool in tools}
    assert "knowledge_search" in names
    assert "skill" not in names
    assert "list_skills" not in names
    assert "load_skill" not in names
    assert "unload_skill" not in names
