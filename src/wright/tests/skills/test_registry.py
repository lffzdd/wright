from pathlib import Path

from ...skills.registry import SkillRegistry
from ...skills.store import write_skill


def test_registry_scan_and_cache_invalidation(tmp_path: Path):
    registry = SkillRegistry(tmp_path)
    assert registry.list_metas() == []
    assert registry.has_skills() is False

    write_skill(
        tmp_path,
        "release-check",
        name="发布前检查",
        description="发布时使用",
        body="先跑测试",
    )
    metas = registry.list_metas()
    assert [meta.id for meta in metas] == ["release-check"]
    assert registry.get("release-check").body == "先跑测试"

    first = registry.list_metas()
    second = registry.list_metas()
    assert first[0].description == second[0].description

    write_skill(
        tmp_path,
        "release-check",
        name="发布前检查",
        description="更新后的描述",
        body="先跑测试，再检查日志",
    )
    updated = registry.list_metas()
    assert updated[0].description == "更新后的描述"
    assert "日志" in registry.get("release-check").body


def test_registry_keyword_filter(tmp_path: Path):
    write_skill(tmp_path, "alpha", name="A", description="发布检查", body="a")
    write_skill(tmp_path, "beta", name="B", description="日常备忘", body="b")
    registry = SkillRegistry(tmp_path)
    found = registry.list_metas("发布")
    assert [meta.id for meta in found] == ["alpha"]


def test_later_directory_fills_missing_ids_without_overriding(tmp_path: Path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    write_skill(project, "shared", name="P", description="项目版", body="project body")
    write_skill(user, "shared", name="U", description="用户版", body="user body")
    write_skill(user, "extra", name="E", description="仅用户", body="user only")
    registry = SkillRegistry([project, user])
    assert registry.get("shared").body == "project body"
    assert registry.get("extra").body == "user only"
    assert {meta.id for meta in registry.list_metas()} == {"shared", "extra"}
