"""Recoverable file transactions for permission stores and their checkpoints."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ..core.paths import user_permission_settings_path, wright_home
from .file_lock import FileLock

_LOCK = threading.RLock()
_STATE = threading.local()
_CHECKPOINT_ROOTS: set[Path] = set()


class PermissionRecoveryError(RuntimeError):
    """No authority may be issued until recovery completes."""
    def __init__(self, message, errors=()):
        self.errors = tuple(errors)
        super().__init__(message + (": " + "; ".join(str(error) for error in errors) if errors else ""))


def register_checkpoint_root(root: Path) -> None:
    with _LOCK:
        _CHECKPOINT_ROOTS.add(root.resolve())


def journal_directory() -> Path:
    return wright_home() / 'permission-transactions'


def _sync_directory(path: Path) -> None:
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _sync_directory(directory.parent)


def _replace(path: Path, content: bytes | None) -> None:
    _ensure_directory(path.parent)
    if content is None:
        path.unlink(missing_ok=True)
        _sync_directory(path.parent)
        return
    descriptor, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_bytes(path: Path) -> bytes | None:
    pending = getattr(_STATE, 'pending', None)
    if pending is not None and path.resolve() in pending:
        return pending[path.resolve()][1]
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def atomic_write(path: Path, content: bytes) -> None:
    path = path.resolve()
    pending = getattr(_STATE, 'pending', None)
    if pending is None:
        with transaction():
            _replace(path, content)
    else:
        _validate_target(path)
        before = pending[path][0] if path in pending else read_bytes(path)
        pending[path] = (before, content)


def _validate_target(path: Path) -> None:
    home = wright_home()
    config = os.environ.get('WRIGHT_PERMISSION_CONFIG')
    settings = {user_permission_settings_path().resolve()}
    if config:
        settings.add(Path(config).expanduser().resolve())
    relative = path.relative_to(home / 'projects') if path.is_relative_to(home / 'projects') else None
    permitted = relative is not None and (
        (len(relative.parts) == 2 and relative.parts[1] == 'permissions.json')
        or (len(relative.parts) == 3 and relative.parts[1] in {'sessions', 'permissions-sessions'} and path.suffix == '.json')
    )
    if path not in settings and not permitted and not any(path.parent == root and path.suffix == '.json' for root in _CHECKPOINT_ROOTS):
        raise PermissionRecoveryError(f'Unregistered transaction target: {path}')


def _encoded(content: bytes | None):
    if content is None:
        return None
    return {'data': base64.b64encode(content).decode('ascii'), 'sha256': hashlib.sha256(content).hexdigest()}


def _decoded(value):
    if value is None:
        return None
    content = base64.b64decode(value['data'], validate=True)
    if hashlib.sha256(content).hexdigest() != value['sha256']:
        raise PermissionRecoveryError('Transaction content checksum mismatch')
    return content


def _write_log(path: Path, data: dict) -> None:
    _replace(path, json.dumps(data, sort_keys=True).encode())


def _recover_log(path: Path) -> None:
    try:
        data = json.loads(path.read_bytes())
        if data.get('version') != 1 or data.get('state') not in {'PREPARED', 'COMMITTED'} or not isinstance(data.get('files'), list):
            raise PermissionRecoveryError('Invalid permission transaction log')
        entries = []
        for item in data['files']:
            target = Path(item['path'])
            if not target.is_absolute() or target.resolve() != target:
                raise PermissionRecoveryError('Transaction target is no longer canonical')
            _validate_target(target)
            before, after = _decoded(item['before']), _decoded(item['after'])
            entries.append((target, after if data['state'] == 'COMMITTED' else before))
        errors = []
        for target, content in entries:
            try:
                _replace(target, content)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise PermissionRecoveryError('Permission transaction recovery failed', errors)
        path.unlink()
        _sync_directory(path.parent)
    except Exception as exc:
        raise PermissionRecoveryError(f'Cannot recover permission transaction {path.name}: {exc}', getattr(exc, 'errors', ())) from exc


def _recover() -> None:
    directory = journal_directory()
    if directory.exists():
        for path in sorted(directory.glob('*.json')):
            _recover_log(path)


@contextmanager
def transaction():
    """The common lock order for authorization and ordinary checkpoint writes."""
    with _LOCK:
        if getattr(_STATE, 'active', False):
            yield
            return
        lock = wright_home() / 'permission-transaction.lock'
        _ensure_directory(lock.parent)
        with FileLock(lock):
            _STATE.active = True
            try:
                _recover()
                yield
            finally:
                _STATE.active = False


@contextmanager
def unit_of_work():
    with transaction():
        if getattr(_STATE, 'pending', None) is not None:
            raise RuntimeError('Nested permission work units are not supported')
        _STATE.pending = {}
        try:
            yield
            pending = _STATE.pending
        finally:
            _STATE.pending = None
        if not pending:
            return
        directory = journal_directory()
        _ensure_directory(directory)
        log = directory / f'{uuid4().hex}.json'
        data = {'version': 1, 'state': 'PREPARED', 'files': [
            {'path': str(path), 'before': _encoded(before), 'after': _encoded(after)}
            for path, (before, after) in pending.items()
        ]}
        _write_log(log, data)
        try:
            for path, (_, after) in pending.items():
                _replace(path, after)
            data['state'] = 'COMMITTED'
            _write_log(log, data)
        except Exception as exc:
            # A failure writing COMMITTED can have completed the rename. The
            # on-disk state determines recovery; never invent a second outcome.
            try:
                _recover_log(log)
            except Exception as recovery:
                raise PermissionRecoveryError('Commit and recovery failed', [exc, recovery]) from exc
            raise
        log.unlink()
        _sync_directory(directory)
