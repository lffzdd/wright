"""Project-scoped episode storage.

New records live under ``episodes/projects/<project_id>/``. Flat ``episodes/ep-*.json``
files stay readable as legacy records with an unknown project. They are not
migrated, deleted, or returned by automatic current-project search.

Ordering uses ``created_at`` and then id. File mtime is not a semantic key.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path

from ....domain.gateway.memory import IEpisodeStore
from ....domain.model.memory import (
    EPISODE_ID_RE,
    EPISODE_SEARCH_SCOPES,
    EPISODE_STATUSES,
    PROJECT_ID_RE,
    EpisodeNotFoundError,
    EpisodeRecord,
    EpisodeSearchHit,
    EpisodeSearchScope,
    EpisodeStatus,
    EpisodeStoreError,
    episode_fact_pieces,
)
from ....domain.policy.memory import has_result_or_verification
from .lexical import bm25_scores, tokenize
from .paths import memory_dir

EPISODES_DIRECTORY = "episodes"
PROJECTS_DIRECTORY = "projects"
MAX_EPISODES = 500
MAX_EPISODE_FILE_BYTES = 256_000
_locks_guard = threading.Lock()
_store_locks: dict[Path, threading.RLock] = {}


class EpisodeStore(IEpisodeStore):
    def __init__(self, memory_directory: Path | None = None) -> None:
        self.root = (memory_directory or memory_dir()).expanduser().resolve()
        self.directory = self.root / EPISODES_DIRECTORY

    def save(self, episode: EpisodeRecord) -> EpisodeRecord:
        checked = EpisodeRecord.from_dict(episode.to_dict())
        with _StoreLock(self.directory):
            existing = self._find_unlocked(checked.id)
            if existing is not None:
                return self._read_path(existing)
            if not checked.project_id:
                raise EpisodeStoreError("新 episode 必须带 project_id")
            path = self._destination(checked)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            text = json.dumps(checked.to_dict(), ensure_ascii=False, indent=2) + "\n"
            if len(text.encode("utf-8")) > MAX_EPISODE_FILE_BYTES:
                raise EpisodeStoreError("episode 文件超出大小上限")
            _atomic_write(path, text)
            if checked.project_id:
                self._prune_project_unlocked(checked.project_id)
        return checked

    def get(self, episode_id: str) -> EpisodeRecord:
        with _StoreLock(self.directory):
            path = self._find_unlocked(episode_id)
            if path is None:
                raise EpisodeNotFoundError(f"episode 不存在: {episode_id}")
            return self._read_path(path)

    def delete(self, episode_id: str) -> EpisodeRecord:
        with _StoreLock(self.directory):
            path = self._find_unlocked(episode_id)
            if path is None:
                raise EpisodeNotFoundError(f"episode 不存在: {episode_id}")
            episode = self._read_path(path)
            try:
                path.unlink()
            except OSError as exc:
                raise EpisodeStoreError(f"episode 删除失败: {exc}") from exc
        return episode

    def list(
        self,
        limit: int = 100,
        *,
        project_id: str | None = None,
        scope: EpisodeSearchScope | None = None,
    ) -> list[EpisodeRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EPISODES:
            raise EpisodeStoreError(f"limit 必须是 1..{MAX_EPISODES} 的整数")
        with _StoreLock(self.directory):
            episodes = self._load_scope(scope=scope, project_id=project_id or "")
        _sort_by_time(episodes)
        return episodes[:limit]

    def recent(self, *, project_id: str, limit: int = 10) -> list[EpisodeRecord]:
        if not project_id:
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EPISODES:
            raise EpisodeStoreError(f"limit 必须是 1..{MAX_EPISODES} 的整数")
        with _StoreLock(self.directory):
            episodes = self._load_scope(scope="current_project", project_id=project_id)
        _sort_by_time(episodes)
        return episodes[:limit]

    def search(
        self,
        query: str = "",
        *,
        status: EpisodeStatus | None = None,
        limit: int = 20,
        scope: EpisodeSearchScope = "current_project",
        project_id: str = "",
    ) -> list[EpisodeSearchHit]:
        if scope not in EPISODE_SEARCH_SCOPES:
            raise EpisodeStoreError("episode scope 非法")
        if status is not None and status not in EPISODE_STATUSES:
            raise EpisodeStoreError("episode status 非法")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise EpisodeStoreError("limit 必须是 1..100 的整数")
        if scope == "current_project" and not project_id:
            return []
        with _StoreLock(self.directory):
            loaded = self._load_scope(scope=scope, project_id=project_id)
        episodes = [
            episode
            for episode in loaded
            if status is None or episode.status == status
        ]
        query_tokens = tokenize(str(query))
        if not query_tokens:
            _sort_by_time(episodes)
            return [
                EpisodeSearchHit(episode=episode, lexical_score=0.0)
                for episode in episodes[:limit]
            ]
        documents = [_weighted_tokens(episode) for episode in episodes]
        scores = bm25_scores(documents, query_tokens)
        hits = [
            EpisodeSearchHit(episode=episode, lexical_score=score)
            for episode, score in zip(episodes, scores, strict=True)
            if score > 0
        ]
        _sort_hits(hits)
        return hits[:limit]

    def path_for(self, episode_id: str) -> Path:
        path = self._find_unlocked(episode_id) if self.directory.is_dir() else None
        if path is None:
            raise EpisodeNotFoundError(f"episode 不存在: {episode_id}")
        return path

    def _destination(self, episode: EpisodeRecord) -> Path:
        if episode.project_id:
            self._check_project_id(episode.project_id)
            return (
                self.directory / PROJECTS_DIRECTORY / episode.project_id / f"{episode.id}.json"
            )
        self._check_episode_id(episode.id)
        return self.directory / f"{episode.id}.json"

    def _find_unlocked(self, episode_id: str) -> Path | None:
        self._check_episode_id(episode_id)
        name = f"{episode_id}.json"
        legacy = self.directory / name
        if _is_regular_file(legacy):
            return legacy
        projects = self.directory / PROJECTS_DIRECTORY
        if not projects.is_dir():
            return None
        for child in projects.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if PROJECT_ID_RE.fullmatch(child.name) is None:
                continue
            path = child / name
            if _is_regular_file(path):
                return path
        return None

    def _load_scope(
        self,
        *,
        scope: EpisodeSearchScope | None,
        project_id: str,
    ) -> list[EpisodeRecord]:
        if scope is not None and scope not in EPISODE_SEARCH_SCOPES:
            raise EpisodeStoreError("episode scope 非法")
        if scope == "legacy":
            paths = self._legacy_files()
        elif scope == "current_project":
            if not project_id:
                return []
            self._check_project_id(project_id)
            paths = self._project_files(project_id)
        elif scope == "all_projects":
            paths = self._all_project_files()
        elif project_id:
            self._check_project_id(project_id)
            paths = self._project_files(project_id)
        else:
            paths = [*self._all_project_files(), *self._legacy_files()]
        episodes: list[EpisodeRecord] = []
        for path in paths:
            try:
                episodes.append(self._read_path(path))
            except EpisodeStoreError:
                continue
        return episodes

    def _legacy_files(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return [
            path
            for path in self.directory.glob("ep-*.json")
            if _is_regular_file(path)
        ]

    def _project_files(self, project_id: str) -> list[Path]:
        directory = self.directory / PROJECTS_DIRECTORY / project_id
        if not directory.is_dir() or directory.is_symlink():
            return []
        return [
            path
            for path in directory.glob("ep-*.json")
            if _is_regular_file(path)
        ]

    def _all_project_files(self) -> list[Path]:
        projects = self.directory / PROJECTS_DIRECTORY
        if not projects.is_dir():
            return []
        paths: list[Path] = []
        for child in projects.iterdir():
            if (
                child.is_dir()
                and not child.is_symlink()
                and PROJECT_ID_RE.fullmatch(child.name) is not None
            ):
                paths.extend(self._project_files(child.name))
        return paths

    def _prune_project_unlocked(self, project_id: str) -> None:
        ranked: list[tuple[str, str, Path]] = []
        for path in self._project_files(project_id):
            try:
                episode = self._read_path(path)
            except EpisodeStoreError:
                continue
            ranked.append((episode.created_at, episode.id, path))
        ranked.sort(key=lambda row: row[1])
        ranked.sort(key=lambda row: row[0], reverse=True)
        for _, _, path in ranked[MAX_EPISODES:]:
            path.unlink(missing_ok=True)

    def _read_path(self, path: Path) -> EpisodeRecord:
        try:
            if path.is_symlink() or not path.is_file():
                raise EpisodeStoreError("拒绝读取符号链接 episode")
            if path.stat().st_size > MAX_EPISODE_FILE_BYTES:
                raise EpisodeStoreError("episode 文件超出大小上限")
            return EpisodeRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except EpisodeStoreError:
            raise
        except FileNotFoundError as exc:
            raise EpisodeNotFoundError(f"episode 不存在: {path.stem}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise EpisodeStoreError(f"episode 无法读取: {exc}") from exc

    @staticmethod
    def _check_episode_id(episode_id: str) -> None:
        if not isinstance(episode_id, str) or EPISODE_ID_RE.fullmatch(episode_id) is None:
            raise EpisodeStoreError("episode_id 非法")

    @staticmethod
    def _check_project_id(project_id: str) -> None:
        if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
            raise EpisodeStoreError("project_id 非法")


def _weighted_tokens(episode: EpisodeRecord) -> list[str]:
    """Field weights: goal 3, outcome 2, errors and verification 2, names 1.

    Token usage and other counters are excluded.
    """
    tokens: list[str] = []

    def add(text: str, weight: int) -> None:
        parts = tokenize(text)
        for _ in range(weight):
            tokens.extend(parts)

    facts = episode_fact_pieces(episode)
    add(episode.goal, 3)
    add(episode.outcome, 2)
    add(facts["errors"], 2)
    add(facts["verification"], 2)
    add(facts["objects"], 2)
    add(facts["names"], 1)
    return tokens


def _sort_by_time(episodes: list[EpisodeRecord]) -> None:
    episodes.sort(key=lambda episode: episode.id)
    episodes.sort(key=lambda episode: episode.created_at, reverse=True)


def _sort_hits(hits: list[EpisodeSearchHit]) -> None:
    hits.sort(key=lambda hit: hit.episode.id)
    hits.sort(key=lambda hit: hit.episode.created_at, reverse=True)
    hits.sort(key=lambda hit: 0 if has_result_or_verification(hit.episode) else 1)
    hits.sort(key=lambda hit: hit.lexical_score, reverse=True)


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _thread_lock(directory: Path) -> threading.RLock:
    with _locks_guard:
        return _store_locks.setdefault(directory, threading.RLock())


class _StoreLock:
    """In-process re-entrant lock plus an exclusive inter-process file lock."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._thread = _thread_lock(directory)
        self._fd: int | None = None

    def __enter__(self) -> _StoreLock:
        self._thread.acquire()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._fd = fd
        except Exception:
            self._thread.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
        finally:
            self._thread.release()


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "EPISODES_DIRECTORY",
    "MAX_EPISODES",
    "EpisodeNotFoundError",
    "EpisodeRecord",
    "EpisodeStatus",
    "EpisodeStore",
    "EpisodeStoreError",
]
