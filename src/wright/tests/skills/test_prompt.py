from pathlib import Path

from ...skills.prompt import catalog_reminder
from ...skills.types import MAX_CATALOG_CHARS, SkillMeta


def _meta(skill_id: str, description: str) -> SkillMeta:
    return SkillMeta(
        id=skill_id,
        name=skill_id,
        description=description,
        allowed_tools=(),
        path=Path(skill_id) / "SKILL.md",
    )


def test_small_catalog_keeps_full_descriptions():
    text = catalog_reminder([
        _meta("release-check", "发布前检查"),
        _meta("review-pr", "审阅拉取请求"),
    ])
    assert "- release-check: 发布前检查" in text
    assert "- review-pr: 审阅拉取请求" in text
    assert "未列出" not in text


def test_over_budget_keeps_every_id_and_truncates_descriptions():
    metas = [_meta(f"skill-{index:02d}", "x" * 200) for index in range(25)]
    text = catalog_reminder(metas)
    assert len(text) <= MAX_CATALOG_CHARS
    assert "未列出" not in text
    for meta in metas:
        assert f"- {meta.id}:" in text
        assert "x" * 200 not in text


def test_tight_budget_falls_back_to_names_only(monkeypatch):
    monkeypatch.setattr("wright.skills.prompt.MAX_CATALOG_CHARS", 400)
    metas = [_meta(f"skill-{index:02d}", "说明" * 30) for index in range(20)]
    text = catalog_reminder(metas)
    assert "未列出" not in text
    for meta in metas:
        assert f"- {meta.id}" in text
        assert f"- {meta.id}:" not in text
