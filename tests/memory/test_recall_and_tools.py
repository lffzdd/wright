"""召回选择器(用假 LLM)+ 记忆工具的单测。"""

import json
from pathlib import Path

from tests.responses import event
from wright.application.memory.memory_service import MemoryService
from wright.domain.model.events import ContentDelta
from wright.infrastructure.llm.context_selector import LlmContextSelector
from wright.infrastructure.persistence.memory import (
    EpisodeStore,
    SemanticMemoryStore,
    write_memory_file,
)
from wright.infrastructure.tools.memory import (
    build_memory_tools,
    create_memory,
    delete_memory,
    get_memory,
    search_memory,
    update_memory,
)


class FakeLLM:
    """假 LLMClient:吐一个携带预设 JSON 的 ContentDone,模拟 side-query。"""

    def __init__(self, payload: dict):
        self._content = json.dumps(payload, ensure_ascii=False)

    def __call__(self, messages, **kwargs):
        yield event(content=self._content, reasoning="")


def _service(tmp_path: Path, llm) -> MemoryService:
    return MemoryService(
        SemanticMemoryStore(tmp_path),
        EpisodeStore(tmp_path),
        selector=LlmContextSelector(llm),
    )


def test_find_relevant_filters_to_valid_filenames(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "x", directory=tmp_path)
    write_memory_file("beta", "about cats", "user", "y", directory=tmp_path)
    # 选择器返回一个合法 + 一个不存在的文件名,后者应被过滤
    llm = FakeLLM({"selected_memories": ["alpha.md", "ghost.md"]})
    context = _service(tmp_path, llm).prepare_memory_context("用 bun")
    assert [record.path.name for record in context.memories] == ["alpha.md"]


def test_find_relevant_empty_on_bad_json(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "x", directory=tmp_path)

    class BadLLM:
        def __call__(self, messages, **kwargs):
            yield event(content="not json", reasoning="")

    context = _service(tmp_path, BadLLM()).prepare_memory_context("q")
    assert context.memories == ()
    assert context.episodes == ()


def test_find_relevant_no_files(tmp_path: Path):
    llm = FakeLLM({"selected_memories": []})
    context = _service(tmp_path, llm).prepare_memory_context("q")
    assert context.memories == ()
    assert context.semantic_text == ""


def test_build_recall_block_wraps_in_reminder(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "正文B", directory=tmp_path)
    from wright.infrastructure.persistence.memory import rebuild_index

    rebuild_index(tmp_path)
    llm = FakeLLM({"selected_memories": ["alpha.md"]})
    context = _service(tmp_path, llm).prepare_memory_context("用 bun 吗")
    block = context.semantic_text
    assert block.startswith("<system-reminder")
    assert block.rstrip().endswith("</system-reminder>")
    assert "正文B" in block  # 选中记忆全文被注入
    assert "MEMORY.md" in block  # 索引也在
    assert "wright-semantic-recall" in block


def test_selector_ignores_streaming_deltas(tmp_path: Path):
    write_memory_file("alpha", "about bun", "feedback", "正文", directory=tmp_path)

    class StreamingLLM:
        def __call__(self, messages, **kwargs):
            yield ContentDelta(piece='{"selected_memories": ["ghost.md"], ')
            yield event(content='{"selected_memories": ["alpha.md"], "selected_episodes": []}')

    context = _service(tmp_path, StreamingLLM()).prepare_memory_context("bun")
    assert [record.path.name for record in context.memories] == ["alpha.md"]


def test_create_memory_tool_writes_and_indexes(tmp_path: Path):
    res = create_memory(
        "user-likes-bun", "prefers bun", "feedback", "用 bun 不用 npm",
        scope="global",
        directory=tmp_path,
    )
    assert res.ok
    assert (tmp_path / f"{res.data['id']}.md").is_file()
    assert "user-likes-bun" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert res.data["scope"] == "global"
    assert res.data["origin"] == "explicit"


def test_create_memory_rejects_bad_type(tmp_path: Path):
    res = create_memory("x", "d", "bogus", "c", directory=tmp_path)
    assert not res.ok
    assert "type" in res.err


def test_search_memory_lists(tmp_path: Path):
    created = create_memory("a", "desc a", "user", "x", scope="global", directory=tmp_path)
    res = search_memory("desc", directory=tmp_path)
    assert res.ok
    assert res.data["count"] == 1
    assert created.data["id"] in res.data["memories"]
    missed = search_memory("anything", directory=tmp_path)
    assert missed.ok and missed.data["count"] == 0


def test_explicit_memory_crud_tools(tmp_path: Path):
    created = create_memory(
        "project-api", "API decision", "project", "use v2", scope="global", directory=tmp_path
    )
    assert created.ok
    memory_id = created.data["id"]
    duplicate = create_memory(
        "project-api", "duplicate", "project", "other body", scope="global", directory=tmp_path
    )
    assert duplicate.ok and duplicate.data["id"] != memory_id
    assert get_memory(memory_id, directory=tmp_path).data["content"] == "use v2"

    fetched = get_memory(memory_id, directory=tmp_path)
    assert fetched.ok and fetched.data["content"] == "use v2"

    updated = update_memory(
        memory_id,
        content="use v3",
        expected_revision=created.data["revision"],
        directory=tmp_path,
    )
    assert updated.ok and updated.data["content"] == "use v3"
    assert updated.data["id"] == memory_id
    assert not update_memory(
        memory_id, expected_revision=updated.data["revision"], directory=tmp_path
    ).ok

    deleted = delete_memory(memory_id, directory=tmp_path)
    assert deleted.ok
    assert not get_memory(memory_id, directory=tmp_path).ok


def test_bound_toolset_uses_explicit_crud(tmp_path: Path):
    tools = build_memory_tools(tmp_path)
    assert [tool.name for tool in tools] == [
        "create_memory", "get_memory", "update_memory", "delete_memory", "search_memory"
    ]
    create = tools[0].call({
        "name": "bound",
        "description": "same directory",
        "type": "project",
        "content": "yes",
        "scope": "global",
    }, None)
    assert create.ok
    assert (tmp_path / f"{create.data['id']}.md").is_file()
