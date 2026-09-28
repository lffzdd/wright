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
from pathlib import Path

from ...core.paths import user_permission_settings_path
from ...domain.policy.permission.settings import PermissionSettings

_CONFIG_LOCK = threading.RLock()


def load_permission_settings(path: Path | None = None) -> PermissionSettings:
    """加载权限配置。

    优先级:显式 path > 环境变量 WRIGHT_PERMISSION_CONFIG > ~/.wright/
    permission_settings.json > 包内默认文件。显式 path 缺失时返回空配置,
    不继续往下找。解析失败直接抛,宁可启动报错也不要静默放行一份坏配置。
    """
    if path is not None:
        if not path.is_file():
            return PermissionSettings()
        return PermissionSettings.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    for candidate in (_env_path(), _user_path(), _packaged_path()):
        if candidate is not None and candidate.is_file():
            return PermissionSettings.from_dict(
                json.loads(candidate.read_text(encoding="utf-8"))
            )
    return PermissionSettings()


def default_settings_path() -> Path:
    """"别再问"写回的路径(env 覆盖 > ~/.wright/permission_settings.json)。

    不写包内默认文件:安装后那份可能只读,也不该被一次本地确认改掉。
    注意它不含显式 path 分支——那是调用方临时指定的,不该被持久化反向写回。
    """
    return _env_path() or _user_path()


def append_allow_rule(rule: str, path: Path | None = None) -> None:
    """把一条规则追加进配置的 permissions.allow 并写回磁盘(去重)。"""

    def update(data: dict) -> None:
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        if rule not in allow:
            allow.append(rule)

    _update_settings(update, path)


def append_additional_directory(directory: str, path: Path | None = None) -> None:
    """Append an extra working directory to permissions.additionalDirectories."""
    resolved = str(Path(directory).expanduser().resolve())

    def update(data: dict) -> None:
        extra = data.setdefault("permissions", {}).setdefault("additionalDirectories", [])
        if resolved not in extra:
            extra.append(resolved)

    _update_settings(update, path)


def _update_settings(update: Callable[[dict], None], path: Path | None) -> None:
    """Read/merge/atomically replace settings under one process lock."""

    target = path or default_settings_path()
    with _CONFIG_LOCK:
        if target.is_file():
            data = json.loads(target.read_text(encoding="utf-8"))
        elif path is None and _packaged_path().is_file():
            data = json.loads(_packaged_path().read_text(encoding="utf-8"))
        else:
            data = {"mode": "default", "permissions": {"allow": [], "deny": []}}
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        update(data)
        if json.dumps(data, ensure_ascii=False, sort_keys=True) == before:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
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
