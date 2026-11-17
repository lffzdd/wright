"""SQLite implementation of the memory store.

Each record is stored as its JSON form plus a few indexed columns used for
filtering. Ranking happens in Python (``domain.policy.memory.lexical``) because
SQLite's FTS tokenizers handle Chinese poorly and the record counts are small.

One connection is shared behind a re-entrant lock. WAL mode and a busy timeout
let several Wright processes use the same file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ....domain.gateway.memory import IMemoryStore
from ....domain.model.memory import (
    ChangeRecord,
    Commitment,
    EpisodeRecord,
    Fact,
    MemoryConflictError,
    MemoryStoreError,
    Space,
)

DATABASE_NAME = "memory.db"
SCHEMA_VERSION = "1"
MAX_EPISODES_PER_PROJECT = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    binding TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    status TEXT NOT NULL,
    tier TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_by_space ON facts(space_id, status);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_by_project ON episodes(project_id, created_at);
CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    status TEXT NOT NULL,
    due TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before TEXT,
    after TEXT,
    note TEXT NOT NULL DEFAULT '',
    reverted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS changes_by_target ON changes(target_id);
"""


class SqliteMemoryStore(IMemoryStore):
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / DATABASE_NAME
        self._lock = threading.RLock()
        self._depth = 0
        try:
            self._conn = sqlite3.connect(
                str(self.path), timeout=10, check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            # executescript commits on its own, so it runs outside transaction().
            # Every statement in the schema is idempotent.
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        except sqlite3.Error as exc:
            raise MemoryStoreError(f"memory database unavailable: {exc}") from exc
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                return self._conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                raise MemoryStoreError(f"memory database error: {exc}") from exc

    # spaces ------------------------------------------------------------

    def upsert_space(self, space: Space) -> Space:
        with self.transaction():
            existing = self.get_space(space.id)
            if existing is not None:
                if space.binding and space.binding != existing.binding:
                    self._execute("UPDATE spaces SET binding = ? WHERE id = ?", (space.binding, space.id))
                    return Space(existing.id, existing.type, existing.name, existing.created_at, space.binding)
                return existing
            self._execute(
                "INSERT INTO spaces(id, type, name, created_at, binding) VALUES (?, ?, ?, ?, ?)",
                (space.id, space.type, space.name, space.created_at, space.binding),
            )
        return space

    def get_space(self, space_id: str) -> Space | None:
        row = self._execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
        return _space(row) if row is not None else None

    def list_spaces(self) -> list[Space]:
        rows = self._execute("SELECT * FROM spaces ORDER BY created_at").fetchall()
        return [_space(row) for row in rows]

    # facts -------------------------------------------------------------

    def insert_fact(self, fact: Fact) -> Fact:
        with self.transaction():
            if self.get_fact(fact.id) is not None:
                raise MemoryStoreError(f"fact already exists: {fact.id}")
            self._execute(
                "INSERT INTO facts(id, space_id, status, tier, revision, updated_at, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fact.id, fact.space_id, fact.status, fact.tier, fact.revision, fact.updated_at, _dump(fact.to_dict())),
            )
        return fact

    def replace_fact(self, fact: Fact, *, expected_revision: int | None = None) -> Fact:
        with self.transaction():
            current = self.get_fact(fact.id)
            if current is None:
                raise MemoryStoreError(f"fact not found: {fact.id}")
            if expected_revision is not None and current.revision != expected_revision:
                raise MemoryConflictError(
                    f"fact {fact.id} is at revision {current.revision}, not {expected_revision}"
                )
            self._execute(
                "UPDATE facts SET space_id = ?, status = ?, tier = ?, revision = ?, updated_at = ?, body = ? "
                "WHERE id = ?",
                (fact.space_id, fact.status, fact.tier, fact.revision, fact.updated_at, _dump(fact.to_dict()), fact.id),
            )
        return fact

    def get_fact(self, fact_id: str) -> Fact | None:
        row = self._execute("SELECT body FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return Fact.from_dict(json.loads(row["body"])) if row is not None else None

    def list_facts(
        self,
        *,
        space_ids: Sequence[str] | None = None,
        statuses: Sequence[str] | None = ("active",),
        tiers: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[Fact]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, values in (("space_id", space_ids), ("status", statuses), ("tier", tiers)):
            if values is None:
                continue
            values = list(values)
            if not values:
                return []
            clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        sql = "SELECT body FROM facts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        facts: list[Fact] = []
        for row in self._execute(sql, params).fetchall():
            try:
                facts.append(Fact.from_dict(json.loads(row["body"])))
            except Exception:
                continue
        return facts

    def touch_facts(self, fact_ids: Sequence[str], at: str) -> None:
        if not fact_ids:
            return
        with self.transaction():
            for fact_id in dict.fromkeys(fact_ids):
                fact = self.get_fact(fact_id)
                if fact is None:
                    continue
                touched = fact.evolve(last_used_at=at, use_count=fact.use_count + 1)
                self._execute("UPDATE facts SET body = ? WHERE id = ?", (_dump(touched.to_dict()), fact_id))

    def purge_fact(self, fact_id: str) -> None:
        self._execute("DELETE FROM facts WHERE id = ?", (fact_id,))

    # episodes ----------------------------------------------------------

    def save_episode(self, episode: EpisodeRecord) -> EpisodeRecord:
        with self.transaction():
            existing = self.get_episode(episode.id)
            if existing is not None:
                return existing
            self._execute(
                "INSERT INTO episodes(id, project_id, created_at, body) VALUES (?, ?, ?, ?)",
                (episode.id, episode.project_id, episode.created_at, _dump(episode.to_dict())),
            )
            self._execute(
                "DELETE FROM episodes WHERE project_id = ? AND id NOT IN ("
                "SELECT id FROM episodes WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT ?)",
                (episode.project_id, episode.project_id, MAX_EPISODES_PER_PROJECT),
            )
        return episode

    def replace_episode(self, episode: EpisodeRecord) -> EpisodeRecord:
        self._execute(
            "UPDATE episodes SET project_id = ?, created_at = ?, body = ? WHERE id = ?",
            (episode.project_id, episode.created_at, _dump(episode.to_dict()), episode.id),
        )
        return episode

    def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        row = self._execute("SELECT body FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if row is None:
            return None
        try:
            return EpisodeRecord.from_dict(json.loads(row["body"]))
        except Exception:
            return None

    def list_episodes(
        self, *, project_ids: Sequence[str] | None = None, limit: int | None = None
    ) -> list[EpisodeRecord]:
        sql = "SELECT body FROM episodes"
        params: list[Any] = []
        if project_ids is not None:
            ids = list(project_ids)
            if not ids:
                return []
            sql += f" WHERE project_id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        episodes: list[EpisodeRecord] = []
        for row in self._execute(sql, params).fetchall():
            try:
                episodes.append(EpisodeRecord.from_dict(json.loads(row["body"])))
            except Exception:
                continue
        return episodes

    def delete_episode(self, episode_id: str) -> bool:
        cursor = self._execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        return cursor.rowcount > 0

    # commitments -------------------------------------------------------

    def save_commitment(self, item: Commitment) -> Commitment:
        self._execute(
            "INSERT INTO commitments(id, space_id, status, due, body) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET space_id = excluded.space_id, status = excluded.status, "
            "due = excluded.due, body = excluded.body",
            (item.id, item.space_id, item.status, item.due, _dump(item.to_dict())),
        )
        return item

    def get_commitment(self, commitment_id: str) -> Commitment | None:
        row = self._execute("SELECT body FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        return Commitment.from_dict(json.loads(row["body"])) if row is not None else None

    def list_commitments(
        self, *, space_ids: Sequence[str] | None = None, statuses: Sequence[str] | None = None
    ) -> list[Commitment]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, values in (("space_id", space_ids), ("status", statuses)):
            if values is None:
                continue
            values = list(values)
            if not values:
                return []
            clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        sql = "SELECT body FROM commitments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE WHEN due = '' THEN 1 ELSE 0 END, due, id"
        return [Commitment.from_dict(json.loads(row["body"])) for row in self._execute(sql, params).fetchall()]

    def delete_commitment(self, commitment_id: str) -> bool:
        return self._execute("DELETE FROM commitments WHERE id = ?", (commitment_id,)).rowcount > 0

    # change log --------------------------------------------------------

    def append_change(
        self,
        *,
        at: str,
        actor: str,
        action: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        note: str = "",
    ) -> ChangeRecord:
        cursor = self._execute(
            "INSERT INTO changes(at, actor, action, target_id, before, after, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, actor, action, target_id, _dump_optional(before), _dump_optional(after), note),
        )
        return ChangeRecord(int(cursor.lastrowid or 0), at, actor, action, target_id, before, after, note)  # type: ignore[arg-type]

    def list_changes(self, *, limit: int = 100, target_id: str = "") -> list[ChangeRecord]:
        if target_id:
            rows = self._execute(
                "SELECT * FROM changes WHERE target_id = ? ORDER BY id DESC LIMIT ?", (target_id, limit)
            ).fetchall()
        else:
            rows = self._execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_change(row) for row in rows]

    def get_change(self, change_id: int) -> ChangeRecord | None:
        row = self._execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        return _change(row) if row is not None else None

    def mark_reverted(self, change_id: int) -> None:
        self._execute("UPDATE changes SET reverted = 1 WHERE id = ?", (change_id,))

    # meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _dump_optional(value: dict[str, Any] | None) -> str | None:
    return None if value is None else _dump(value)


def _space(row: sqlite3.Row) -> Space:
    return Space(row["id"], row["type"], row["name"], row["created_at"], row["binding"] or "")


def _change(row: sqlite3.Row) -> ChangeRecord:
    return ChangeRecord(
        id=int(row["id"]),
        at=row["at"],
        actor=row["actor"],
        action=row["action"],
        target_id=row["target_id"],
        before=json.loads(row["before"]) if row["before"] else None,
        after=json.loads(row["after"]) if row["after"] else None,
        note=row["note"] or "",
        reverted=bool(row["reverted"]),
    )


__all__ = ["DATABASE_NAME", "MAX_EPISODES_PER_PROJECT", "SqliteMemoryStore"]
