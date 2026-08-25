"""进程级、只读磁盘的 Skill 注册表：带缓存和失效检测。"""

from __future__ import annotations

from pathlib import Path
import threading

from .store import load_skill_file, normalize_skill_id, scan_skills, skill_file_path
from .types import SkillDefinition, SkillMeta, SkillNotFoundError, SkillStoreError


class SkillRegistry:
    """同一 directory 上线程安全的只读视图。会话注入状态不在这里。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self._lock = threading.RLock()
        self._skills: dict[str, SkillDefinition] = {}
        self._errors: tuple[str, ...] = ()
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None

    def has_skills(self) -> bool:
        return bool(self.list_metas())

    def list_metas(self, query: str = "") -> list[SkillMeta]:
        needle = query.strip().casefold()
        with self._lock:
            self._refresh_unlocked()
            metas = [item.meta for item in self._skills.values()]
        if not needle:
            return metas
        return [
            meta
            for meta in metas
            if needle in meta.id.casefold()
            or needle in meta.name.casefold()
            or needle in meta.description.casefold()
        ]

    def get(self, skill_id: str) -> SkillDefinition:
        normalized = normalize_skill_id(skill_id)
        with self._lock:
            self._refresh_unlocked()
            found = self._skills.get(normalized)
            if found is not None:
                return found
        # 缓存未命中时再读一次磁盘，给调用方可操作的错误。
        path = skill_file_path(self.directory, normalized)
        if not path.is_file():
            raise SkillNotFoundError(f"未知 skill: {normalized}")
        try:
            definition = load_skill_file(path, normalized)
        except SkillStoreError:
            raise
        with self._lock:
            self._fingerprint = None
            self._refresh_unlocked()
        return definition

    def scan_errors(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh_unlocked()
            return self._errors

    def _refresh_unlocked(self) -> None:
        fingerprint = self._current_fingerprint()
        if fingerprint == self._fingerprint:
            return
        definitions, errors = scan_skills(self.directory)
        self._skills = {item.id: item for item in definitions}
        self._errors = tuple(errors)
        self._fingerprint = fingerprint

    def _current_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        if not self.directory.is_dir():
            return ()
        rows: list[tuple[str, int, int]] = []
        try:
            children = list(self.directory.iterdir())
        except OSError:
            return ()
        for child in children:
            skill_path = child / "SKILL.md"
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
                if skill_path.is_symlink() or not skill_path.is_file():
                    continue
                stat = skill_path.stat()
            except OSError:
                continue
            rows.append((child.name, stat.st_mtime_ns, stat.st_size))
        rows.sort()
        return tuple(rows)
