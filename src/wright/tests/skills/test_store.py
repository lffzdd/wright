from pathlib import Path

import pytest

from ...skills.store import (
    load_skill_file,
    normalize_skill_id,
    scan_skills,
    skill_file_path,
    write_skill,
)
from ...skills.types import MAX_SKILL_FILE_BYTES, SkillStoreError


def _valid_markdown() -> str:
    return (
        "---\n"
        "name: 发布前检查\n"
        "description: 当用户提到发布时使用\n"
        "allowed_tools: [execute_command, read_file]\n"
        "---\n\n"
        "先跑测试再发布。\n"
    )


def test_parse_frontmatter_and_allowed_tools(tmp_path: Path):
    path = write_skill(
        tmp_path,
        "release-check",
        name="发布前检查",
        description="当用户提到发布时使用",
        body="先跑测试再发布。",
        allowed_tools=["execute_command", "read_file"],
    )
    definition = load_skill_file(path, "release-check")
    assert definition.meta.name == "发布前检查"
    assert definition.meta.description == "当用户提到发布时使用"
    assert definition.meta.allowed_tools == ("execute_command", "read_file")
    assert "先跑测试" in definition.body


def test_missing_name_or_description_is_rejected(tmp_path: Path):
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text("---\nname: only-name\n---\nbody\n", encoding="utf-8")
    with pytest.raises(SkillStoreError, match="description"):
        load_skill_file(path, "broken")


def test_skill_id_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(SkillStoreError, match="路径"):
        normalize_skill_id("../outside")
    with pytest.raises(SkillStoreError, match="路径"):
        skill_file_path(tmp_path, "..")
    with pytest.raises(SkillStoreError):
        normalize_skill_id("a/b")


def test_symlink_skill_is_rejected(tmp_path: Path):
    target_dir = tmp_path / "outside"
    target_dir.mkdir()
    target = target_dir / "SKILL.md"
    target.write_text(_valid_markdown(), encoding="utf-8")

    linked = tmp_path / "skills" / "linked"
    linked.parent.mkdir()
    linked.symlink_to(target_dir)
    definitions, errors = scan_skills(tmp_path / "skills")
    assert definitions == []
    assert any("符号链接" in item for item in errors)

    real = tmp_path / "skills" / "real"
    real.mkdir()
    (real / "SKILL.md").symlink_to(target)
    definitions, errors = scan_skills(tmp_path / "skills")
    assert all(item.id != "real" for item in definitions)
    assert any("符号链接" in item for item in errors)


def test_oversized_file_is_rejected(tmp_path: Path):
    skill_dir = tmp_path / "huge"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_bytes(b"x" * (MAX_SKILL_FILE_BYTES + 1))
    with pytest.raises(SkillStoreError, match="过大"):
        load_skill_file(path, "huge")


def test_one_bad_skill_does_not_break_the_rest(tmp_path: Path):
    write_skill(
        tmp_path,
        "good",
        name="好的",
        description="可用",
        body="步骤",
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")
    definitions, errors = scan_skills(tmp_path)
    assert [item.id for item in definitions] == ["good"]
    assert any("bad" in item for item in errors)
