"""In-memory permission repository for explicit, isolated composition."""
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from ...domain.policy.permission.settings import PermissionSettings
from .permission_store import protected_permission_paths


class MemoryPermissionRepository:
    def __init__(self, settings: PermissionSettings, project):
        self._settings = deepcopy(settings)
        self.project = project
        self.project_identity = 'memory-project'
        self._sessions = {}
        self._revision = 0
        self._session_revisions = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            yield

    @contextmanager
    def unit_of_work(self):
        with self._lock:
            before = deepcopy((self._settings, self._sessions, self._revision, self._session_revisions))
            try:
                yield
            except Exception:
                self._settings, self._sessions, self._revision, self._session_revisions = before
                raise

    def revision_token(self, identities):
        return (self._revision, tuple((identity, self._session_revisions.get(identity, 0)) for identity in identities),
            tuple(str(path) for path in protected_permission_paths(self.project)))

    def read(self):
        settings = deepcopy(self._settings)
        settings.revision = str(self._revision)
        return settings, tuple(str(path) for path in protected_permission_paths(self.project))

    def read_session(self, identity, initial):
        if identity not in self._sessions:
            self.write_session(identity, initial)
        return deepcopy(self._sessions[identity]), str(self._session_revisions[identity])

    def write_session(self, identity, records):
        if self._sessions.get(identity) != list(records):
            self._sessions[identity] = deepcopy(list(records))
            self._session_revisions[identity] = self._session_revisions.get(identity, 0) + 1

    def add(self, records, lifetime):
        from ...domain.policy.permission.settings import PermissionRule
        for record in records:
            rule = PermissionRule.from_mapping(record)
            if not any(item.id == rule.id for item in self._settings.allow):
                self._settings.allow.append(rule)
                if lifetime == 'project':
                    self._settings.project_rules.append(rule)
                self._revision += 1

    def remove(self, identity, lifetime):
        self._settings.allow = [rule for rule in self._settings.allow if rule.id != identity or rule.lifetime != lifetime]
        self._settings.project_rules = [rule for rule in self._settings.project_rules if rule.id != identity]
        self._revision += 1
