"""store.py 的纯文件操作单测(不碰 LLM)。"""

from pathlib import Path

from ...memory.store import (
    MemoryAlreadyExistsError,
    MemoryNotFoundError,
    create_memory,
    delete_memory,
    MAX_INDEX_LINES,
    dump_frontmatter,
    format_manifest,
    parse_frontmatter,
    read_entrypoint,
    read_memories_for_surfacing,
    rebuild_index,
    scan_memory_files,
    search_memories,
    slugify,
    update_memory,
    write_memory_file,
)
import pytest


def test_frontmatter_round_trip():
    text = dump_frontmatter(
        "user-role", "user is a data scientist", "user", "正文内容\n第二行"
    )
    fm, body = parse_frontmatter(text)
    assert fm["name"] == "user-role"
    assert fm["description"] == "user is a data scientist"
    assert fm["type"] == "user"
    assert body.strip() == "正文内容\n第二行"


def test_parse_frontmatter_missing_returns_empty():
    fm, body = parse_frontmatter("没有 frontmatter 的纯文本")
    assert fm == {}
    assert body == "没有 frontmatter 的纯文本"


def test_slugify():
    assert slugify("User Role!") == "user-role"
    assert slugify("  多个   空格 ") != ""  # 非空兜底
    assert slugify("!!!") == "memory"


def test_scan_sorts_newest_first_and_skips_index(tmp_path: Path):
    import os
    import time

    write_memory_file("old", "old one", "user", "x", directory=tmp_path)
    time.sleep(0.01)
    write_memory_file("new", "new one", "feedback", "y", directory=tmp_path)
    # MEMORY.md 必须被跳过
    (tmp_path / "MEMORY.md").write_text("- [old](old.md)\n", encoding="utf-8")

    headers = scan_memory_files(tmp_path)
    names = [h.filename for h in headers]
    assert "MEMORY.md" not in names
    assert names == ["new.md", "old.md"]  # mtime 倒序
    assert headers[0].type == "feedback"
    assert headers[0].description == "new one"


def test_rebuild_index_lists_each_memory(tmp_path: Path):
    write_memory_file("alpha", "first", "user", "a", directory=tmp_path)
    write_memory_file("beta", "second", "project", "b", directory=tmp_path)
    index_path = rebuild_index(tmp_path)
    content = index_path.read_text(encoding="utf-8")
    assert "[alpha](alpha.md) — first" in content
    assert "[beta](beta.md) — second" in content


def test_rebuild_index_empty(tmp_path: Path):
    rebuild_index(tmp_path)
    content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "暂无记忆" in content


def test_read_entrypoint_truncates(tmp_path: Path):
    lines = "\n".join(f"- line {i}" for i in range(MAX_INDEX_LINES + 50))
    (tmp_path / "MEMORY.md").write_text(lines, encoding="utf-8")
    out = read_entrypoint(tmp_path)
    assert "警告" in out
    # 截断后正文行数不超过上限
    body_lines = [l for l in out.splitlines() if l.startswith("- line")]
    assert len(body_lines) <= MAX_INDEX_LINES


def test_read_entrypoint_missing_returns_empty(tmp_path: Path):
    assert read_entrypoint(tmp_path) == ""


def test_format_manifest(tmp_path: Path):
    write_memory_file("a", "desc a", "user", "x", directory=tmp_path)
    manifest = format_manifest(scan_memory_files(tmp_path))
    assert "[user] a.md: desc a" in manifest


def test_read_memories_for_surfacing(tmp_path: Path):
    p = write_memory_file("a", "desc", "user", "正文 X", directory=tmp_path)
    block = read_memories_for_surfacing([p])
    assert "### a.md" in block
    assert "正文 X" in block


def test_semantic_memory_crud_keeps_stable_unicode_id_and_index(tmp_path: Path):
    created = create_memory(
        "用户偏好", "用户偏好的包管理器", "user", "优先使用 uv", tmp_path
    )
    assert created.id == "用户偏好"
    assert created.path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(MemoryAlreadyExistsError):
        create_memory("用户偏好", "重复", "user", "不要覆盖", tmp_path)

    updated = update_memory(
        created.id,
        name="Python 工具偏好",
        description="更新后的描述",
        content="优先使用 uv，除非项目明确要求其它工具",
        directory=tmp_path,
    )
    assert updated.id == created.id  # 改展示名不会破坏已有引用
    assert updated.name == "Python 工具偏好"
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at
    assert search_memories("uv", type_="user", directory=tmp_path) == [updated]

    deleted = delete_memory(created.id, tmp_path)
    assert deleted.id == created.id
    assert "用户偏好.md" not in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    with pytest.raises(MemoryNotFoundError):
        delete_memory(created.id, tmp_path)


def test_memory_id_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(Exception, match="memory_id"):
        update_memory("../outside", content="x", directory=tmp_path)
