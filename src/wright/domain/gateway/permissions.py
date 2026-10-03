"""Persistence port for permission records; policy never performs I/O."""

from contextlib import AbstractContextManager
from typing import Protocol

from ..policy.permission.settings import PermissionSettings


class PermissionRepository(Protocol):
    def transaction(self) -> AbstractContextManager: ...

    def read(self) -> tuple[PermissionSettings, tuple[str, ...]]: ...

    def add(self, records: tuple[dict, ...], lifetime: str) -> None: ...

    def remove(self, record_id: str, lifetime: str) -> None: ...

    def read_session(self, session_id: str, initial: tuple[dict, ...]) -> tuple[list[dict], str]: ...

    def write_session(self, session_id: str, records: tuple[dict, ...]) -> None: ...
