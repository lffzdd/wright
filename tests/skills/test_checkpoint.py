import json
from pathlib import Path

from wright.domain.model.session import Session
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.storage.skills import write_skill


def test_checkpoint_does_not_store_catalog_flag_or_skill_body(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_skill(
        workspace / "skills",
        "release-check",
        name="release-check",
        description="发布时使用",
        body="正文只留在被调用后的 transcript，checkpoint 不另存",
    )
    session = Session.create("goal", workspace)
    store = FileSessionRepository(tmp_path / "checkpoints")
    path = store.save(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "skill_catalog_sent" not in payload["session"]
    assert "active_skill_ids" not in payload["session"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "正文只留在被调用后的 transcript" not in dumped

    store.load(session.session_id)
