"""进程级、只读磁盘的 Skill 注册表：带缓存和失效检测。"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

from .store import load_skill_file, normalize_skill_id, scan_skills, skill_file_path
from .types import SkillDefinition, SkillMeta, SkillNotFoundError, SkillStoreError


class SkillRegistry:
    """一组 skill 目录上的线程安全只读视图。前面的目录同 id 优先。会话注入状态不在这里。"""

    def __init__(self, directory: Path | str | Sequence[Path | str]) -> None:
        if isinstance(directory, (str, Path)):
            dirs = [Path(directory)]
        else:
            dirs = [Path(item) for item in directory]
        if not dirs:
            raise ValueError("SkillRegistry 至少需要一个目录")
        self.directories = tuple(path.expanduser().resolve() for path in dirs)
        self.directory = self.directories[0]
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
        last_error: SkillStoreError | None = None
        for directory in self.directories:
            path = skill_file_path(directory, normalized)
            if not path.is_file():
                continue
            try:
                definition = load_skill_file(path, normalized)
            except SkillStoreError as exc:
                last_error = exc
                continue
            with self._lock:
                self._fingerprint = None
                self._refresh_unlocked()
            return definition
        if last_error is not None:
            raise last_error
        raise SkillNotFoundError(f"Unknown skill: {normalized}")

    def scan_errors(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh_unlocked()
            return self._errors

    def _refresh_unlocked(self) -> None:
        fingerprint = self._current_fingerprint()
        if fingerprint == self._fingerprint:
            return
        merged: dict[str, SkillDefinition] = {}
        errors: list[str] = []
        for directory in self.directories:
            definitions, dir_errors = scan_skills(directory)
            errors.extend(dir_errors)
            for item in definitions:
                merged.setdefault(item.id, item)
        self._skills = merged
        self._errors = tuple(errors)
        self._fingerprint = fingerprint

    def _current_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        rows: list[tuple[str, int, int]] = []
        for directory in self.directories:
            if not directory.is_dir():
                continue
            try:
                children = list(directory.iterdir())
            except OSError:
                continue
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
                rows.append((f"{directory}:{child.name}", stat.st_mtime_ns, stat.st_size))
        rows.sort()
        return tuple(rows)
