"""Read and atomically update the permission settings file.

Rule meaning stays in the domain. This module only resolves which file to
use and replaces it without a partial write.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ...core.paths import user_permission_settings_path
from ...domain.policy.permission.settings import PermissionRule, PermissionSettings
from ..permission_transaction import (
    _ensure_directory,
    atomic_write,
    journal_directory,
    read_bytes,
    transaction,
    unit_of_work,
)

# Older installs copied these as user-level deny. They are now ask, except
# cleartext HTTP which follows the same origin approval as HTTPS.
_LEGACY_STOCK_DENY = frozenset({
    "shell(rm *)",
    "shell(sudo *)",
    "shell(* sudo *)",
    "shell(*git push*)",
    "http_request(http://*)",
})
_STOCK_ASK = (
    "shell(rm *)",
    "shell(sudo *)",
    "shell(* sudo *)",
    "shell(*git push*)",
)
# Command-text globs for filesystem root and home. These are not a path check:
# expanded home paths and other absolute targets still need a later matcher.
_STOCK_DENY = (
    "shell(rm -* /)",
    "shell(rm -* /[*])",
    "shell(rm -* /.)",
    "shell(rm -* /..)",
    "shell(rm -* ~)",
    "shell(rm -* ~/)",
    "shell(rm -* ~[*])",
    "shell(rm -* ~/[*])",
    "shell(rm -* $HOME)",
    "shell(rm -* $HOME/)",
    "shell(rm -* $HOME[*])",
    "shell(rm -* $HOME/[*])",
    "shell(rm /)",
    "shell(rm ~)",
    "shell(rm $HOME)",
    "shell(rmdir /)",
    "shell(rmdir ~)",
    "shell(rmdir $HOME)",
)
PACKAGED_RULE_SOURCE = "builtin"


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
    """Merge settings under the common recovery and transaction lock."""
    target = path or default_settings_path()
    with transaction():
        current = read_bytes(target)
        if current is not None:
            data = json.loads(current)
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
        atomic_write(target, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def _env_path() -> Path | None:
    raw = os.getenv("WRIGHT_PERMISSION_CONFIG")
    return Path(raw).expanduser().resolve() if raw else None


def _user_path() -> Path:
    return user_permission_settings_path()


def _packaged_path() -> Path:
    return Path(__file__).resolve().parent / "permission_settings.json"


def protected_permission_paths(project: Path | None = None) -> tuple[Path, ...]:
    from ...core.paths import (
        project_permission_settings_path,
        user_mcp_config_path,
        user_preferences_path,
        user_skills_dir,
        workspace_registry_path,
        wright_home,
    )

    home = wright_home()
    paths = [default_settings_path(), _user_path(), _packaged_path(),
             home / "sandbox", home / "projects", home / "permission-transaction.lock", journal_directory(),
             home / ".env", user_mcp_config_path(), user_skills_dir(), home / "memory", user_preferences_path(),
             workspace_registry_path(), home / "backups"]
    if project is not None:
        paths.append(project_permission_settings_path(project))
    return tuple(dict.fromkeys(path.resolve() for path in paths))


class FilePermissionRepository:
    """Authoritative user, project and session stores with light revision probes."""

    def __init__(self, project: Path | None):
        """``project=None`` reads and edits only the user layer (no project is open)."""
        from ...core.paths import project_id, project_permission_settings_path

        self.project_path = project_permission_settings_path(project) if project is not None else None
        self.project = project
        self.project_identity = project_id(project) if project is not None else ""

    def transaction(self):
        return transaction()

    def unit_of_work(self):
        return unit_of_work()

    def read(self) -> tuple[PermissionSettings, tuple[str, ...]]:
        # Materialize protected boundaries before issuing any execution permit.
        # This also prevents creating a previously absent permission file via Shell.
        from ...core.paths import wright_home
        for directory in (wright_home() / "projects", wright_home() / "sandbox"):
            _ensure_directory(directory)
        if not default_settings_path().exists():
            _update_settings(lambda _: None, None)
        if not _user_path().exists():
            _update_settings(lambda _: None, _user_path())
        for path in dict.fromkeys((default_settings_path(), _user_path())):
            if path.is_file():
                _update_settings(_rewrite_legacy_stock_denies, path)
        settings = load_permission_settings()
        if any(rule.kind == "shell" for rule in settings.allow):
            raise ValueError("Shell grants can only be stored in a conversation")
        if any(rule.root_kind == "workspace" for rule in settings.allow):
            raise ValueError("User authorizations require absolute resource roots")
        settings.allow = [_stamp_lifetime(rule, "user") for rule in settings.allow]
        settings.ask = [_stamp_lifetime(rule, "user") for rule in settings.ask]
        settings.deny = _merge_packaged_denies(settings.deny)
        if self.project_path is not None and self.project_path.is_file():
            data = json.loads(self.project_path.read_text(encoding="utf-8"))
            if data.get("version") != 2:
                raise ValueError("Project permissions are outdated; approve access again")
            project = PermissionSettings.from_dict(data)
            settings.revision += ":" + project.revision
            if any(rule.kind == "shell" for rule in project.allow):
                raise ValueError("Shell grants cannot be stored for a project")
            if any(rule.project_id and rule.project_id != self.project_identity for rule in project.allow):
                raise ValueError("Authorization belongs to a different project")
            settings.project_rules = [_stamp_lifetime(rule, "project") for rule in project.allow]
            settings.allow.extend(settings.project_rules)
            packaged_keys = {_rule_restriction_key(rule) for rule in settings.deny if rule.source == PACKAGED_RULE_SOURCE}
            settings.deny.extend(
                _stamp_lifetime(rule, "project") for rule in project.deny
                if _rule_restriction_key(rule) not in packaged_keys
            )
            settings.ask.extend(_stamp_lifetime(rule, "project") for rule in project.ask)
        return settings, self.protected_paths()

    def protected_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in protected_permission_paths(self.project))

    def revision_token(self, identities: tuple[str, ...]):
        project = (self.project_path,) if self.project_path is not None else ()
        paths = (default_settings_path(), _user_path(), _packaged_path(), *project, *(self._session_path(identity) for identity in identities))
        def stamp(path):
            try:
                info = path.stat()
                return (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except FileNotFoundError:
                return (str(path), None)
        return (tuple(stamp(path) for path in paths), tuple(str(p) for p in protected_permission_paths(self.project)))

    def _session_path(self, identity: str) -> Path:
        if self.project_path is None:
            raise ValueError("Session permissions need a project")
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

        _update_settings(update, self._layer_path(lifetime))

    def remove(self, record_id: str, lifetime: str) -> None:
        def update(data: dict) -> None:
            permissions = data.setdefault("permissions", {})
            for bucket in ("allow", "deny", "ask"):
                items = permissions.setdefault(bucket, [])
                permissions[bucket] = [
                    item for item in items if _stored_identity(item, lifetime, bucket) != record_id
                ]

        _update_settings(update, self._layer_path(lifetime))

    def set_default_mode(self, mode: str) -> None:
        """The mode new sessions start in; only the user layer holds it."""

        def update(data: dict) -> None:
            data["mode"] = mode

        _update_settings(update, None)

    def add_restriction(self, text: str, effect: str, lifetime: str) -> None:
        """Append a deny or ask rule in its text form. Only tightens access."""
        if effect not in {"deny", "ask"}:
            raise ValueError("Only deny and ask rules can be added here")

        def update(data: dict) -> None:
            bucket = data.setdefault("permissions", {}).setdefault(effect, [])
            if text not in bucket:
                bucket.append(text)

        _update_settings(update, self._layer_path(lifetime))

    def _layer_path(self, lifetime: str) -> Path | None:
        if lifetime != "project":
            return None
        if self.project_path is None:
            raise ValueError("No project is open")
        return self.project_path


def _configuration_id(rule, lifetime: str) -> str:
    import hashlib
    from dataclasses import replace
    payload = replace(rule, lifetime=lifetime, id="").to_persistent()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def _stamp_lifetime(rule: PermissionRule, lifetime: str) -> PermissionRule:
    return replace(rule, lifetime=lifetime, id=rule.id or _configuration_id(rule, lifetime))


def _packaged_settings() -> PermissionSettings:
    path = _packaged_path()
    if not path.is_file():
        return PermissionSettings()
    return PermissionSettings.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _rule_restriction_key(rule: PermissionRule) -> str:
    if rule.kind == "network" and rule.scheme:
        return f"{rule.tool_name}({rule.scheme}://*)"
    if rule.kind == "shell":
        return f"{rule.tool_name}({rule.pattern})"
    return f"{rule.tool_name}({rule.subject_glob or rule.pattern})"


def _merge_packaged_denies(user_denies: list[PermissionRule]) -> list[PermissionRule]:
    """Packaged deny rules stay locked. User copies of the same pattern are dropped."""
    packaged = [
        _stamp_lifetime(replace(rule, source=PACKAGED_RULE_SOURCE), "user")
        for rule in _packaged_settings().deny
    ]
    packaged_keys = {_rule_restriction_key(rule) for rule in packaged}
    extra = [
        _stamp_lifetime(rule, "user") for rule in user_denies
        if _rule_restriction_key(rule) not in packaged_keys
    ]
    return packaged + extra


def _stored_identity(item: object, lifetime: str, effect: str) -> str:
    if isinstance(item, str):
        rule = PermissionRule.parse(item, effect=effect)
    elif isinstance(item, dict):
        rule = PermissionRule.from_mapping(item)
        if rule.id:
            return str(rule.id)
    else:
        raise TypeError("permission rule must be a string or an object")
    return _configuration_id(rule, lifetime)


def _restriction_key(item: object, effect: str) -> str:
    if isinstance(item, str):
        rule = PermissionRule.parse(item, effect=effect)
    elif isinstance(item, dict):
        rule = PermissionRule.from_mapping(item)
    else:
        raise TypeError("permission rule must be a string or an object")
    return _rule_restriction_key(rule)


def _rewrite_legacy_stock_denies(data: dict) -> None:
    permissions = data.setdefault("permissions", {})
    deny = permissions.get("deny") or []
    ask = permissions.get("ask") or []
    try:
        deny_keys = [_restriction_key(item, "deny") for item in deny]
        stock_deny = {_restriction_key(item, "deny") for item in _STOCK_DENY}
    except (TypeError, ValueError):
        return
    if not ask and set(deny_keys) == _LEGACY_STOCK_DENY:
        permissions["deny"] = []
        permissions["ask"] = list(_STOCK_ASK)
        return
    kept = [item for item, key in zip(deny, deny_keys, strict=True) if key not in stock_deny]
    if len(kept) != len(deny):
        permissions["deny"] = kept
