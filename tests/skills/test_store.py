from pathlib import Path

import pytest

from wright.domain.model.skills import MAX_SKILL_FILE_BYTES, SkillStoreError
from wright.infrastructure.storage.skills import (
    load_skill_file,
    normalize_skill_id,
    scan_skills,
    skill_file_path,
    write_skill,
)


def _valid_markdown() -> str:
    return (
        "---\n"
        "name: release-check\n"
        "description: 当用户提到发布时使用\n"
        "allowed-tools: [execute_command, read_file]\n"
        "---\n\n"
        "先跑测试再发布。\n"
    )


def test_parse_frontmatter_and_allowed_tools(tmp_path: Path):
    path = write_skill(
        tmp_path,
        "release-check",
        name="release-check",
        description="当用户提到发布时使用",
        body="先跑测试再发布。",
        allowed_tools=["execute_command", "read_file"],
    )
    definition = load_skill_file(path, "release-check")
    assert definition.meta.name == "release-check"
    assert definition.meta.description == "当用户提到发布时使用"
    assert definition.meta.allowed_tools == ("execute_command", "read_file")
    assert "先跑测试" in definition.body


def test_missing_name_or_description_is_rejected(tmp_path: Path):
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text("---\nname: broken\n---\nbody\n", encoding="utf-8")
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


def test_standard_multiline_frontmatter(tmp_path: Path):
    skill_dir = tmp_path / "release-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: release-check\n"
        "description: >-\n"
        "  release deployment checks\n"
        "  across lines\n"
        "license: Apache-2.0\n"
        "compatibility: Requires git\n"
        "metadata:\n"
        "  author: wright\n"
        "  nested:\n"
        "    kind: procedure\n"
        "allowed-tools: execute_command read_file\n"
        "---\n\n"
        "Run scripts/check.py\n",
        encoding="utf-8",
    )
    definition = load_skill_file(skill_dir / "SKILL.md", "release-check")
    assert definition.meta.name == "release-check"
    assert definition.meta.description == "release deployment checks across lines"
    assert definition.meta.allowed_tools == ("execute_command", "read_file")
    assert definition.meta.license == "Apache-2.0"
    assert definition.meta.metadata_map()["nested"] == {"kind": "procedure"}
    assert "scripts/check.py" in definition.body


def test_name_must_match_directory_and_old_tool_field_is_rejected(tmp_path: Path):
    mismatched = tmp_path / "release-check" / "SKILL.md"
    mismatched.parent.mkdir()
    mismatched.write_text(
        "---\nname: 发布前检查\ndescription: 中文显示名\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillStoreError, match="目录名"):
        load_skill_file(mismatched, "release-check")

    legacy = tmp_path / "legacy" / "SKILL.md"
    legacy.parent.mkdir()
    legacy.write_text(
        "---\n"
        "name: legacy\n"
        "description: 旧字段\n"
        "allowed_tools: [execute_command]\n"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillStoreError, match="allowed-tools"):
        load_skill_file(legacy, "legacy")


def test_duplicate_frontmatter_key_is_rejected(tmp_path: Path):
    path = tmp_path / "release-check" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(
        "---\n"
        "name: release-check\n"
        "description: first\n"
        "description: second\n"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillStoreError, match="重复"):
        load_skill_file(path, "release-check")


def test_malformed_yaml_utf8_and_unreadable_files_stay_isolated(tmp_path: Path):
    write_skill(tmp_path, "good", name="good", description="可用", body="步骤")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: [not closed\ndescription: bad\n---\nbody\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid-utf8"
    invalid.mkdir()
    (invalid / "SKILL.md").write_bytes(
        b"---\nname: x\ndescription: y\n---\n\xff\xfe"
    )
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked_file = blocked / "SKILL.md"
    blocked_file.write_text(
        "---\nname: blocked\ndescription: hidden\n---\nbody\n",
        encoding="utf-8",
    )
    blocked_file.chmod(0)
    try:
        definitions, errors = scan_skills(tmp_path)
    finally:
        blocked_file.chmod(0o644)
    assert [item.id for item in definitions] == ["good"]
    assert any("broken" in item for item in errors)
    assert any("invalid-utf8" in item and "UTF-8" in item for item in errors)
    assert any("blocked" in item for item in errors)


def test_one_bad_skill_does_not_break_the_rest(tmp_path: Path):
    write_skill(
        tmp_path,
        "good",
        name="good",
        description="可用",
        body="步骤",
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")
    definitions, errors = scan_skills(tmp_path)
    assert [item.id for item in definitions] == ["good"]
    assert any("bad" in item for item in errors)
