import json
from pathlib import Path

import pytest

from ...checkpoint import CheckpointError, SessionCheckpointStore
from ...session import SessionState
from ...skills.store import write_skill


def test_checkpoint_round_trips_catalog_flag_not_bodies(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_skill(
        workspace / "skills",
        "release-check",
        name="发布前检查",
        description="发布时使用",
        body="旧正文，checkpoint 不应保存它",
    )
    session = SessionState.create("goal", workspace)
    session.mark_skill_catalog_sent()
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    path = store.save(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session"]["skill_catalog_sent"] is True
    assert "active_skill_ids" not in payload["session"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "旧正文" not in dumped

    restored = store.load(session.session_id)
    assert restored.skill_catalog_sent is True


def test_old_checkpoint_without_catalog_flag_still_loads(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("goal", workspace)
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    path = store.save(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["session"].pop("skill_catalog_sent", None)
    payload["session"]["active_skill_ids"] = ["release-check"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    restored = store.load(session.session_id)
    assert restored.skill_catalog_sent is False


def test_checkpoint_rejects_non_boolean_catalog_flag(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("goal", workspace)
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    path = store.save(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["session"]["skill_catalog_sent"] = "yes"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CheckpointError, match="skill_catalog_sent"):
        store.load(session.session_id)
