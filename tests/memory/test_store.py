from __future__ import annotations

import pytest

from tests.memory.helpers import episode
from wright.domain.model.memory import Fact, MemoryConflictError, Space
from wright.infrastructure.persistence.memory import SqliteMemoryStore
from wright.infrastructure.persistence.memory.sqlite_store import (
    MAX_EPISODES_PER_PROJECT,
)


def _fact(fact_id: str = "f-0000000000000001", **changes) -> Fact:
    base = Fact(
        id=fact_id, space_id="global", kind="preference", subject="user", content="Answer in Chinese.",
        origin="user_stated", sensitivity="personal", tier="indexed", status="active",
        created_at="2026-11-01T00:00:00+00:00", updated_at="2026-11-01T00:00:00+00:00",
    )
    return base.evolve(**changes)


def test_store_round_trips_and_reopens(tmp_path):
    store = SqliteMemoryStore(tmp_path)
    store.upsert_space(Space("global", "global", "global", "t"))
    store.insert_fact(_fact())
    store.close()
    reopened = SqliteMemoryStore(tmp_path)
    assert reopened.get_fact("f-0000000000000001").content == "Answer in Chinese."
    assert [item.id for item in reopened.list_spaces()] == ["global"]


def test_stale_revision_is_refused(tmp_path):
    store = SqliteMemoryStore(tmp_path)
    store.insert_fact(_fact())
    store.replace_fact(_fact(content="v2", revision=2), expected_revision=1)
    with pytest.raises(MemoryConflictError):
        store.replace_fact(_fact(content="v3", revision=3), expected_revision=1)
    assert store.get_fact("f-0000000000000001").content == "v2"


def test_failed_transaction_leaves_nothing_behind(tmp_path):
    store = SqliteMemoryStore(tmp_path)
    with pytest.raises(RuntimeError), store.transaction():
        store.insert_fact(_fact())
        raise RuntimeError("boom")
    assert store.get_fact("f-0000000000000001") is None


def test_touch_does_not_bump_revision(tmp_path):
    store = SqliteMemoryStore(tmp_path)
    store.insert_fact(_fact())
    store.touch_facts(["f-0000000000000001"], "2026-11-02T00:00:00+00:00")
    fact = store.get_fact("f-0000000000000001")
    assert (fact.use_count, fact.revision, fact.last_used_at) == (1, 1, "2026-11-02T00:00:00+00:00")


def test_episodes_insert_once_and_keep_the_newest(tmp_path):
    store = SqliteMemoryStore(tmp_path)
    first = episode("ep-a", goal="first")
    store.save_episode(first)
    assert store.save_episode(episode("ep-a", goal="second")).goal == "first"
    for index in range(MAX_EPISODES_PER_PROJECT + 2):
        store.save_episode(episode(f"ep-{index:04d}", created_at=f"2026-10-{(index % 28) + 1:02d}T{index % 24:02d}:00:00+00:00"))
    assert len(store.list_episodes(project_ids=["proj-a"])) == MAX_EPISODES_PER_PROJECT
