"""Session-owned authorization state. Persistence and decisions live elsewhere."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Literal

from ...policy.permission.settings import PermissionRule

InteractionMode = Literal["agent", "plan", "ask"]
PermissionMode = Literal["default", "acceptEdits", "bypass", "plan"]


@dataclass(frozen=True)
class AuthorizationState:
    session_id: str
    rules: tuple[PermissionRule, ...]
    permission_mode: PermissionMode | None
    interaction_mode: InteractionMode


class SessionAuthorization:
    """One conversation's grants and limits, with live ancestor inheritance."""

    # Topology changes are rare; a shared lock prevents concurrent cycle creation.
    _topology_lock = threading.RLock()

    def __init__(
        self,
        session_id: str,
        rules: Iterable[PermissionRule] = (),
        *,
        permission_mode: PermissionMode | None = None,
        interaction_mode: InteractionMode = "agent",
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Authorization requires a session identity")
        self._session_id = session_id
        self._lock = threading.RLock()
        self._parent: SessionAuthorization | None = None
        self._rules: tuple[PermissionRule, ...] = ()
        self._permission_mode: PermissionMode | None = None
        self._interaction_mode: InteractionMode = "agent"
        self.replace_rules(rules)
        self.set_modes(
            permission_mode=permission_mode, interaction_mode=interaction_mode
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        with self._lock:
            return self._rules

    @property
    def permission_mode(self) -> PermissionMode | None:
        with self._lock:
            return self._permission_mode

    @property
    def interaction_mode(self) -> InteractionMode:
        with self._lock:
            return self._interaction_mode

    def set_modes(
        self,
        *,
        permission_mode: PermissionMode | None,
        interaction_mode: InteractionMode,
    ) -> None:
        if permission_mode is not None and permission_mode not in {
            "default",
            "acceptEdits",
            "bypass",
            "plan",
        }:
            raise ValueError(
                "permission_mode must be default, acceptEdits, bypass, or plan"
            )
        if interaction_mode not in {"agent", "plan", "ask"}:
            raise ValueError("interaction_mode must be agent, plan, or ask")
        with self._lock:
            self._permission_mode = permission_mode
            self._interaction_mode = interaction_mode

    @staticmethod
    def _validate_rule(rule: PermissionRule) -> None:
        if not isinstance(rule, PermissionRule):
            raise TypeError("Session authorization requires PermissionRule values")
        if not rule.id or rule.lifetime != "session":
            raise ValueError("Session rules require a stable id and session lifetime")
        rule.validate_grant()

    def replace_rules(self, rules: Iterable[PermissionRule]) -> None:
        values = tuple(rules)
        identities: set[str] = set()
        for rule in values:
            self._validate_rule(rule)
            if rule.id in identities:
                raise ValueError("Duplicate session authorization id")
            identities.add(rule.id)
        with self._lock:
            self._rules = values

    def add_rule(self, rule: PermissionRule) -> None:
        self._validate_rule(rule)
        with self._lock:
            existing = next((item for item in self._rules if item.id == rule.id), None)
            if existing is not None and existing != rule:
                raise ValueError("Authorization id already belongs to another rule")
            if existing is None:
                self._rules = (*self._rules, rule)

    def revoke(self, identity: str) -> bool:
        with self._lock:
            before = self._rules
            self._rules = tuple(rule for rule in before if rule.id != identity)
            return before != self._rules

    def bind_parent(self, parent: SessionAuthorization | None) -> None:
        if parent is not None and not isinstance(parent, SessionAuthorization):
            raise TypeError("Authorization parent must be SessionAuthorization")
        with self._topology_lock:
            if parent is not None:
                ancestors = parent.lineage()
                if self in ancestors:
                    raise ValueError("Authorization inheritance cannot contain a cycle")
                if any(
                    ancestor.session_id == self.session_id for ancestor in ancestors
                ):
                    raise ValueError(
                        "Authorization inheritance cannot repeat a session identity"
                    )
            self._parent = parent

    def lineage(self) -> tuple[SessionAuthorization, ...]:
        with self._topology_lock:
            ancestors = []
            current: SessionAuthorization | None = self
            while current is not None:
                ancestors.append(current)
                current = current._parent
            return tuple(ancestors)

    def owner(self) -> SessionAuthorization:
        return self.lineage()[-1]

    def snapshot(self) -> AuthorizationState:
        with self._lock:
            return AuthorizationState(
                self.session_id,
                self._rules,
                self._permission_mode,
                self._interaction_mode,
            )

    @contextmanager
    def locked_lineage(self):
        """Keep topology and every authorization state coherent during capture."""
        with self._topology_lock, ExitStack() as locks:
            nodes = self.lineage()
            for node in reversed(nodes):
                locks.enter_context(node._lock)
            yield nodes

    @staticmethod
    def combine_modes(states: tuple[AuthorizationState, ...], default: str) -> tuple[str, InteractionMode]:
        mode = next(
            (
                state.permission_mode
                for state in reversed(states)
                if state.permission_mode
            ),
            default,
        )
        if any(state.permission_mode == "plan" for state in states):
            mode = "plan"
        interaction = (
            "ask"
            if any(state.interaction_mode == "ask" for state in states)
            else "plan"
            if any(state.interaction_mode == "plan" for state in states)
            else states[0].interaction_mode
        )
        return mode, interaction

    def effective_modes(self, default: str) -> tuple[str, InteractionMode]:
        with self.locked_lineage() as nodes:
            return self.combine_modes(tuple(node.snapshot() for node in nodes), default)
