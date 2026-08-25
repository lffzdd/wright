"""召回选择器(用假 LLM)+ 记忆工具的单测。"""

import json
from pathlib import Path

from ...events import ContentDone
from ...memory.recall import build_recall_block, find_relevant_memories
from ...memory.store import write_memory_file
from ...tools.memory_tools import (
    build_memory_tools,
    create_memory,
    delete_memory,
    get_memory,
    save_memory,
    search_memory,
    update_memory,
)


class FakeLLM:
    """假 LLMClient:吐一个携带预设 JSON 的 ContentDone,模拟 side-query。"""

    def __init__(self, payload: dict):
        self._content = json.dumps(payload, ensure_ascii=False)

    def __call__(self, messages):
        yield ContentDone(content=self._content, reasoning="")


def test_find_relevant_filters_to_valid_filenames(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "x", directory=tmp_path)
    write_memory_file("beta", "about cats", "user", "y", directory=tmp_path)
    # 选择器返回一个合法 + 一个不存在的文件名,后者应被过滤
    llm = FakeLLM({"selected_memories": ["alpha.md", "ghost.md"]})
    paths = find_relevant_memories("用 bun", llm, directory=tmp_path)
    assert [p.name for p in paths] == ["alpha.md"]


def test_find_relevant_empty_on_bad_json(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "x", directory=tmp_path)

    class BadLLM:
        def __call__(self, messages):
            yield ContentDone(content="not json", reasoning="")

    assert find_relevant_memories("q", BadLLM(), directory=tmp_path) == []


def test_find_relevant_no_files(tmp_path: Path):
    llm = FakeLLM({"selected_memories": []})
    assert find_relevant_memories("q", llm, directory=tmp_path) == []


def test_build_recall_block_wraps_in_reminder(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "正文B", directory=tmp_path)
    from ...memory.store import rebuild_index

    rebuild_index(tmp_path)
    llm = FakeLLM({"selected_memories": ["alpha.md"]})
    block = build_recall_block("用 bun 吗", llm, directory=tmp_path)
    assert block.startswith("<system-reminder>")
    assert block.rstrip().endswith("</system-reminder>")
    assert "正文B" in block  # 选中记忆全文被注入
    assert "MEMORY.md" in block  # 索引也在


def test_save_memory_tool_writes_and_indexes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_MEMORY_DIR", str(tmp_path))
    res = save_memory("user-likes-bun", "prefers bun", "feedback", "用 bun 不用 npm")
    assert res.ok
    assert (tmp_path / "user-likes-bun.md").is_file()
    assert "user-likes-bun" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_save_memory_rejects_bad_type(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_MEMORY_DIR", str(tmp_path))
    res = save_memory("x", "d", "bogus", "c")
    assert not res.ok
    assert "type" in res.err


def test_search_memory_lists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_MEMORY_DIR", str(tmp_path))
    save_memory("a", "desc a", "user", "x")
    res = search_memory("anything")
    assert res.ok
    assert res.data["count"] == 1
    assert "a.md" in res.data["memories"]


def test_explicit_memory_crud_tools(tmp_path: Path):
    created = create_memory("project-api", "API decision", "project", "use v2", directory=tmp_path)
    assert created.ok
    memory_id = created.data["id"]
    assert not create_memory("project-api", "duplicate", "project", "x", directory=tmp_path).ok

    fetched = get_memory(memory_id, directory=tmp_path)
    assert fetched.ok and fetched.data["content"] == "use v2"

    updated = update_memory(memory_id, content="use v3", directory=tmp_path)
    assert updated.ok and updated.data["content"] == "use v3"
    assert not update_memory(memory_id, directory=tmp_path).ok

    deleted = delete_memory(memory_id, directory=tmp_path)
    assert deleted.ok
    assert not get_memory(memory_id, directory=tmp_path).ok


def test_bound_toolset_uses_explicit_crud_without_legacy_upsert(tmp_path: Path):
    tools = build_memory_tools(tmp_path, include_legacy_save=False)
    assert [tool.name for tool in tools] == [
        "create_memory", "get_memory", "update_memory", "delete_memory", "search_memory"
    ]
    create = tools[0].call({
        "name": "bound", "description": "same directory", "type": "project", "content": "yes"
    }, None)
    assert create.ok
    assert (tmp_path / "bound.md").is_file()
