"""Read and atomically update the permission settings file.

Rule meaning stays in the domain. This module only resolves which file to
use and replaces it without a partial write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ...core.paths import user_permission_settings_path
from ...domain.policy.permission.settings import PermissionSettings
from ..file_lock import FileLock

_CONFIG_LOCK = threading.RLock()
_TRANSACTION_STATE = threading.local()


def load_permission_settings(path: Path | None = None) -> PermissionSettings:
    """加载权限配置。

    优先级:显式 path > 环境变量 WRIGHT_PERMISSION_CONFIG > ~/.wright/
    permission_settings.json > 包内默认文件。显式 path 缺失时返回空配置,
    不继续往下找。解析失败直接抛,宁可启动报错也不要静默放行一份坏配置。
    """
    if path is not None:
        if not path.is_file():
            return PermissionSettings()
        settings = PermissionSettings.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
        settings.managed = True
        return settings
    for candidate in (_env_path(), _user_path(), _packaged_path()):
        if candidate is not None and candidate.is_file():
            settings = PermissionSettings.from_dict(
                json.loads(candidate.read_text(encoding="utf-8"))
            )
            settings.managed = True
            return settings
    return PermissionSettings()


def default_settings_path() -> Path:
    """"别再问"写回的路径(env 覆盖 > ~/.wright/permission_settings.json)。

    不写包内默认文件:安装后那份可能只读,也不该被一次本地确认改掉。
    注意它不含显式 path 分支——那是调用方临时指定的,不该被持久化反向写回。
    """
    return _env_path() or _user_path()


def append_allow_rule(rule: str | dict, path: Path | None = None) -> None:
    """把一条规则追加进配置的 permissions.allow 并写回磁盘(去重)。"""

    def update(data: dict) -> None:
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        if rule not in allow:
            allow.append(rule)

    _update_settings(update, path)


def remove_allow_rule(rule: dict, path: Path | None = None) -> None:
    """Remove one persisted allow rule. Other rules stay in place."""

    def update(data: dict) -> None:
        permissions = data.setdefault("permissions", {})
        allow = permissions.setdefault("allow", [])
        permissions["allow"] = [item for item in allow if item != rule]

    _update_settings(update, path)


def _update_settings(update: Callable[[dict], None], path: Path | None) -> None:
    """Read, merge and atomically replace settings.

    The cross-platform file lock is the cross-process critical section. The in-process
    lock only keeps threads from opening the lock file twice. Neither lock
    is a transaction by itself; the exclusive lock plus one ``os.replace``
    is what keeps a concurrent update from being lost.
    """

    target = path or default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with _CONFIG_LOCK:
        with FileLock(lock_path):
            if target.is_file():
                data = json.loads(target.read_text(encoding="utf-8"))
            elif path is None and _packaged_path().is_file():
                data = json.loads(_packaged_path().read_text(encoding="utf-8"))
            else:
                data = {"version": 2, "mode": "default", "permissions": {"allow": [], "deny": []}}
            PermissionSettings.from_dict(data)
            before = json.dumps(data, ensure_ascii=False, sort_keys=True)
            update(data)
            PermissionSettings.from_dict(data)
            if target.is_file() and json.dumps(data, ensure_ascii=False, sort_keys=True) == before:
                return
            data["revision"] = uuid4().hex
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise


def _env_path() -> Path | None:
    raw = os.getenv("WRIGHT_PERMISSION_CONFIG")
    return Path(raw).expanduser().resolve() if raw else None


def _user_path() -> Path:
    return user_permission_settings_path()


def _packaged_path() -> Path:
    return Path(__file__).resolve().parent / "permission_settings.json"


def protected_permission_paths(project: Path | None = None) -> tuple[Path, ...]:
    from ...core.paths import project_permission_settings_path, wright_home

    paths = [default_settings_path(), _user_path(), _packaged_path(),
             wright_home() / "sandbox", wright_home() / "projects", wright_home() / "permission-transaction.lock"]
    if project is not None:
        paths.append(project_permission_settings_path(project))
    return tuple(dict.fromkeys(path.resolve() for path in paths))


class FilePermissionRepository:
    """Two trusted stores outside the checkout, read on every resolution."""

    def __init__(self, project: Path):
        from ...core.paths import project_permission_settings_path

        self.project_path = project_permission_settings_path(project)
        self.project = project
        from ...core.paths import project_id
        self.project_identity = project_id(project)

    @contextmanager
    def transaction(self):
        from ...core.paths import wright_home
        target = wright_home() / "permission-transaction.lock"
        target.parent.mkdir(parents=True, exist_ok=True)
        with _CONFIG_LOCK:
            if getattr(_TRANSACTION_STATE, "active", False):
                yield
                return
            with FileLock(target):
                _TRANSACTION_STATE.active = True
                try:
                    yield
                finally:
                    _TRANSACTION_STATE.active = False

    def read(self) -> tuple[PermissionSettings, tuple[str, ...]]:
        from dataclasses import replace

        # Materialize protected boundaries before issuing any execution permit.
        # This also prevents creating a previously absent permission file via Shell.
        from ...core.paths import wright_home
        for directory in (wright_home() / "projects", wright_home() / "sandbox"):
            directory.mkdir(parents=True, exist_ok=True)
        if not default_settings_path().exists():
            _update_settings(lambda _: None, None)
        if not _user_path().exists():
            _update_settings(lambda _: None, _user_path())
        settings = load_permission_settings()
        if any(rule.kind == "shell" for rule in settings.allow):
            raise ValueError("Shell grants can only be stored in a conversation")
        if any(rule.root_kind == "workspace" for rule in settings.allow):
            raise ValueError("User authorizations require absolute resource roots")
        settings.allow = [replace(rule, lifetime="user", id=rule.id or _configuration_id(rule, "user")) for rule in settings.allow]
        settings.deny = [replace(rule, lifetime="user") for rule in settings.deny]
        settings.ask = [replace(rule, lifetime="user") for rule in settings.ask]
        if self.project_path.is_file():
            data = json.loads(self.project_path.read_text(encoding="utf-8"))
            if data.get("version") != 2:
                raise ValueError("Project permissions are outdated; approve access again")
            project = PermissionSettings.from_dict(data)
            settings.revision += ":" + project.revision
            if any(rule.kind == "shell" for rule in project.allow):
                raise ValueError("Shell grants cannot be stored for a project")
            if any(rule.project_id and rule.project_id != self.project_identity for rule in project.allow):
                raise ValueError("Authorization belongs to a different project")
            settings.project_rules = [replace(rule, lifetime="project", id=rule.id or _configuration_id(rule, "project")) for rule in project.allow]
            settings.allow.extend(settings.project_rules)
            settings.deny.extend(replace(rule, lifetime="project") for rule in project.deny)
            settings.ask.extend(replace(rule, lifetime="project") for rule in project.ask)
        return settings, tuple(str(path) for path in protected_permission_paths(self.project))

    def _session_path(self, identity: str) -> Path:
        if not identity or not all(char.isalnum() or char in "-_" for char in identity):
            raise ValueError("Invalid session permission identity")
        return self.project_path.parent / "permissions-sessions" / f"{identity}.json"

    def read_session(self, session_id: str, initial: tuple[dict, ...]) -> tuple[list[dict], str]:
        path = self._session_path(session_id)
        if not path.exists():
            self.write_session(session_id, initial)
        data = json.loads(path.read_text(encoding="utf-8"))
        settings = PermissionSettings.from_dict(data)
        records = [rule.to_persistent() for rule in settings.allow]
        if any(not rule.id or rule.lifetime != "session" for rule in settings.allow):
            raise ValueError("Invalid session permission store")
        return records, settings.revision

    def write_session(self, session_id: str, records: tuple[dict, ...]) -> None:
        def update(data):
            data["permissions"]["allow"] = list(records)
        _update_settings(update, self._session_path(session_id))

    def add(self, records: tuple[dict, ...], lifetime: str) -> None:
        def update(data: dict) -> None:
            allow = data.setdefault("permissions", {}).setdefault("allow", [])
            for record in records:
                if not any(isinstance(item, dict) and item.get("id") == record["id"] for item in allow):
                    allow.append(record)

        _update_settings(update, self.project_path if lifetime == "project" else None)

    def remove(self, record_id: str, lifetime: str) -> None:
        def update(data: dict) -> None:
            allow = data.setdefault("permissions", {}).setdefault("allow", [])
            def identity(item):
                from ...domain.policy.permission.settings import PermissionRule
                rule = PermissionRule.from_persistent(item).to_persistent()
                return rule.get("id") or _configuration_id(PermissionRule.from_mapping(rule), lifetime)

            data["permissions"]["allow"] = [item for item in allow if identity(item) != record_id]

        _update_settings(update, self.project_path if lifetime == "project" else None)


def _configuration_id(rule, lifetime: str) -> str:
    import hashlib
    from dataclasses import replace
    payload = replace(rule, lifetime=lifetime, id="").to_persistent()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
