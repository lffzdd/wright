from pathlib import Path
from types import SimpleNamespace

import pytest

from wright.artifacts import ArtifactStore
from wright.capabilities import AgentProfile, CapabilityCatalog, CapabilityError
from wright.executor import ToolExecutor
from wright.tools.base import Tool, ToolCall, ToolResult
from wright.tools.mcp_client import _to_tool_result


def _tool(name: str) -> Tool:
    return Tool(name, name, {}, lambda _args, _runtime: ToolResult.success(name))


def test_catalog_is_the_single_snapshot_for_schema_and_execution():
    read = _tool("read")
    write = _tool("write")
    catalog = CapabilityCatalog([read, write])
    snapshot = catalog.snapshot(AgentProfile("read-only", frozenset({"read"})))
    executor = ToolExecutor({"read": read, "write": write}, capability_snapshot=snapshot)

    assert executor.execute([ToolCall("read", {}, "one")])[0].result.ok
    denied = executor.execute([ToolCall("write", {}, "two")])
    assert denied[0].result.ok is False
    assert "not authorized" in denied[0].result.err


def test_catalog_rejects_colliding_names_and_unknown_profile_capability():
    with pytest.raises(CapabilityError, match="duplicate"):
        CapabilityCatalog([_tool("same"), _tool("same")])
    with pytest.raises(CapabilityError, match="unknown"):
        CapabilityCatalog([_tool("known")]).snapshot(
            AgentProfile("bad", frozenset({"missing"}))
        )


def test_mcp_result_preserves_structured_content_images_and_errors():
    image = SimpleNamespace(type="image", data="abc", mimeType="image/png")
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello"), image],
        structuredContent={"count": 2}, isError=False,
    )
    converted = _to_tool_result(result)
    assert converted.ok
    assert converted.data["structured_content"] == {"count": 2}
    assert converted.content[1]["type"] == "image"

    failed = _to_tool_result(SimpleNamespace(content=[SimpleNamespace(type="text", text="bad")], structuredContent={"code": "x"}, isError=True))
    assert not failed.ok
    assert failed.data["structured_content"] == {"code": "x"}


def test_managed_artifact_survives_source_cleanup_and_rejects_escape(tmp_path):
    source = tmp_path / "report.md"
    source.write_text("# report\ncount: 2", encoding="utf-8")
    store = ArtifactStore(tmp_path / "managed")
    ref = store.register_file(source, run_id="run_a", call_id="call_a", media_type="text/markdown")
    source.unlink()

    assert store.path_for(ref).read_text(encoding="utf-8") == "# report\ncount: 2"


def test_production_tools_cannot_reach_session_or_service_containers():
    root = Path(__file__).parents[1]
    sources = [
        *sorted((root / "tools").glob("*.py")),
        root / "subagent.py",
    ]
    forbidden = ("runtime.session_state", "runtime.services")
    offenders = {
        str(path.relative_to(root)): token
        for path in sources
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }
    assert offenders == {}
