"""MemoryManager: the agent's collaborator for long-term memory.

The agent binds the current project and calls this object at turn boundaries.
Recall, persistence, and episode tools are delegated to MemoryService.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.logger import get_logger
from ...core.paths import project_id
from ...domain.model.memory import EpisodeRecord, EpisodeStoreError
from ...domain.policy.memory import (
    EpisodePolicy,
    SemanticExtractPolicy,
    is_delivered_answer,
)
from .dto import MemoryContextDTO
from .episode import snapshot_from_session
from .extract import extract_from_snapshot
from .llm_util import metered_events, run_side_query
from .meter import MemoryMeter
from .prompt import build_memory_instructions, project_core_memory
from .turn_snapshot import (
    apply_task_update,
    live_agent_tasks,
    sync_snapshot_agents,
)

if TYPE_CHECKING:
    from ...infrastructure.llm.llm import LLMClient
    from .memory_service import MemoryService

logger = get_logger(__name__)


class MemoryManager:
    """Long-term memory collaborator for the root agent."""

    def __init__(
        self,
        llm: LLMClient,
        service: MemoryService,
        directory: Path,
        *,
        selector_llm: LLMClient | None = None,
        episode_policy: EpisodePolicy | None = None,
        semantic_policy: SemanticExtractPolicy | None = None,
        session_repository: Any = None,
    ) -> None:
        self.usage_observer = None
        self.llm = llm
        self.selector_llm = selector_llm or llm
        self.episode_policy = episode_policy or EpisodePolicy()
        self.semantic_policy = semantic_policy or SemanticExtractPolicy()
        self.session_repository = session_repository
        self.meter = MemoryMeter()
        self._side_call: dict[str, Any] | None = None
        self.directory = directory
        self._service = service
        self._project_root: Path | None = None
        self._recall_by_turn: dict[str, MemoryContextDTO] = {}

    @property
    def episode_store(self):
        return self._service.episode_store

    @property
    def current_project_id(self) -> str:
        if self._project_root is None:
            return ""
        return project_id(self._project_root)

    def bind_project(self, project_root: Path) -> None:
        """Bind the session's stable project root. cwd is not consulted.

        A new project cannot keep the previous project's recall cache.
        """
        self._project_root = Path(project_root).expanduser().resolve()
        self._recall_by_turn.clear()

    @property
    def service(self) -> MemoryService:
        return self._service

    def _query(self, messages, **kwargs):
        started = time.monotonic()
        box: dict[str, Any] = {
            "usage": None,
            "model": str(getattr(self.selector_llm, "model", "") or ""),
        }
        self._side_call = box

        def observe(usage):
            box["usage"] = usage
            self._forward_usage(usage)

        try:
            yield from metered_events(self.selector_llm(messages, **kwargs), observe)
        finally:
            box["duration_ms"] = (time.monotonic() - started) * 1000

    def _forward_usage(self, usage) -> None:
        observer = self.usage_observer
        if observer is None:
            return
        try:
            observer(usage)
        except Exception:
            logger.debug("usage observer failed", exc_info=True)

    def _side_query(self, system: str, user: str):
        return run_side_query(
            self.selector_llm,
            system,
            user,
            self._forward_usage,
            model=str(getattr(self.selector_llm, "model", "") or ""),
        )

    def instructions(self) -> str:
        """Static memory instructions for the system prompt."""
        return build_memory_instructions(self.directory)

    def project_system_prompt(self, system_prompt: str) -> str:
        """Read current core memory for one request without changing history."""
        core_block = ""
        try:
            memory = self.service.get_core_memory(self.current_project_id)
            if memory is not None:
                core_block = memory.render_block()
        except Exception as exc:
            logger.info("core_memory_read failure_type=%s", type(exc).__name__)
        return project_core_memory(system_prompt, core_block)

    def recall_for_turn(self, session_state: Any) -> MemoryContextDTO:
        """Recall once per user turn. A semantic write refreshes text without a new selector."""
        key = self._recall_key(session_state)
        current = project_id(_project_root_of(session_state))
        cached = self._recall_by_turn.get(key) if key else None
        if cached is not None and cached.project_id != current:
            cached = None
            self._recall_by_turn.pop(key, None)
        if cached is not None and cached.semantic_generation == self.service.semantic_generation:
            return cached
        if cached is not None:
            context = self.service.reproject_semantic(cached)
            self._recall_by_turn[key] = context
            return context
        context = self._prepare_context(_goal(session_state), project_id=current)
        if key:
            self._recall_by_turn[key] = context
        return context

    def recall_block(self, query: str) -> str:
        """Best-effort recall text. Selector failure does not inject episodes."""
        return self._prepare_context(query, project_id=self.current_project_id).prompt_injection

    def note_injection(self, context: MemoryContextDTO | None, *, omitted: bool) -> None:
        """Record what this main-model request actually carried."""
        if context is None:
            self.meter.note_injection(omitted=omitted)
            return
        self.meter.note_injection(
            selected_episode_ids=context.selected_episode_ids,
            rendered_episode_ids=context.rendered_episode_ids,
            budget_skipped_ids=context.budget_skipped_episode_ids,
            semantic_estimated_tokens=context.semantic_estimated_tokens,
            episode_estimated_tokens=context.episode_estimated_tokens,
            omitted=omitted,
        )

    def extract(self, session_state: Any) -> int:
        """Extract from the active turn snapshot. Does not read a later turn."""
        try:
            snapshot = snapshot_from_session(session_state, None)
        except Exception as exc:
            logger.info("memory_extract failure_type=%s", type(exc).__name__)
            return 0
        return self._extract_snapshot(session_state, snapshot)

    def record_episode(
        self, session_state: Any, final_answer: str | None
    ) -> EpisodeRecord | None:
        """Persist one finished turn when admission allows it."""
        outcome = self.finalize_turn(
            session_state, final_answer, extract_semantic=False
        )
        episode_id = outcome.get("episode_id")
        if not isinstance(episode_id, str):
            return None
        try:
            return self.episode_store.get(episode_id)
        except EpisodeStoreError:
            return None

    def finalize_turn(
        self,
        session_state: Any,
        final_answer: str | None,
        *,
        extract_semantic: bool,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        """Persist or defer the episode, then optionally extract semantics."""
        if getattr(session_state, "agent_task_id", None):
            return {"episode_id": None, "semantic_memories_written": 0, "pending": False}
        snapshot, pending, kept = self._capture(
            session_state, final_answer, termination_reason
        )
        episode_id = None
        if snapshot is None or not kept:
            pending = False
        elif pending:
            if extract_semantic:
                snapshot["extract_requested"] = True
            self._upsert_pending(session_state, snapshot)
            logger.info(
                "episode_deferred root_run_id=%s",
                snapshot.get("episode", {}).get("root_run_id", ""),
            )
        else:
            saved = self._persist_snapshot(session_state, snapshot)
            if saved is not None:
                episode_id = saved.id
                pending = False
            else:
                pending = self._pending_for_root(
                    session_state,
                    str(snapshot.get("episode", {}).get("root_run_id") or ""),
                ) is not None
        extracted = 0
        if extract_semantic and snapshot is not None:
            if pending:
                self._defer_extract(session_state, snapshot)
            else:
                extracted = self._extract_snapshot(session_state, snapshot)
        return {
            "episode_id": episode_id,
            "semantic_memories_written": extracted,
            "pending": pending,
        }

    def settle_previous_turn(
        self,
        session_state: Any,
        *,
        root_turn_id: str,
        task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an older pending snapshot without reading the new turn."""
        snapshot = self._pending_for_turn(session_state, root_turn_id)
        if snapshot is None:
            return {"episode_id": None, "pending": False}
        if isinstance(task, dict):
            apply_task_update(snapshot, task)
        sync_snapshot_agents(snapshot, session_state)
        if live_agent_tasks(session_state, root_turn_id):
            self._upsert_pending(session_state, snapshot)
            return {"episode_id": None, "pending": True}
        saved = self._persist_snapshot(session_state, snapshot)
        written = 0
        if saved is not None and snapshot.get("extract_requested"):
            written = self._extract_snapshot(session_state, snapshot)
        return {
            "episode_id": saved.id if saved is not None else None,
            "semantic_memories_written": written,
            "pending": saved is None and self._pending_for_turn(session_state, root_turn_id) is not None,
        }

    def recover_pending(self, session_state: Any) -> None:
        """Finish snapshots whose agents are already terminal. Keep the rest."""
        for snapshot in list(getattr(session_state, "pending_episode_finalizes", [])):
            root_turn_id = str(snapshot.get("root_turn_id") or "")
            sync_snapshot_agents(snapshot, session_state)
            if live_agent_tasks(session_state, root_turn_id):
                self._upsert_pending(session_state, snapshot)
                continue
            saved = self._persist_snapshot(session_state, snapshot)
            if saved is not None and snapshot.get("extract_requested"):
                self._extract_snapshot(session_state, snapshot)

    def _prepare_context(self, task: str, *, project_id: str) -> MemoryContextDTO:
        self._side_call = None
        try:
            context = self.service.prepare_memory_context(task, project_id=project_id)
        except Exception as exc:
            logger.info("episode_recall failure_type=%s", type(exc).__name__)
            usage = (self._side_call or {}).get("usage")
            self.meter.record_model_call(
                "selection",
                status="failed",
                reason_codes=(type(exc).__name__,),
                attempts=1,
                duration_ms=(self._side_call or {}).get("duration_ms"),
                model=(self._side_call or {}).get("model", ""),
                usage=usage,
            )
            return MemoryContextDTO()
        self._record_selection(context)
        return context

    def _record_selection(self, context: MemoryContextDTO) -> None:
        if not context.selector_attempted:
            self.meter.record_model_call(
                "selection",
                status="skipped",
                reason_codes=("no_selector_call",),
                attempts=0,
            )
            return
        call = self._side_call or {}
        reason = context.selector_failure_type if context.selector_failed else ""
        self.meter.record_model_call(
            "selection",
            status="failed" if context.selector_failed else "succeeded",
            reason_codes=(reason,) if reason else (),
            attempts=1,
            duration_ms=call.get("duration_ms"),
            model=str(call.get("model") or ""),
            usage=call.get("usage"),
        )

    def _defer_extract(self, session_state: Any, snapshot: dict[str, Any]) -> None:
        root_run_id = str(snapshot.get("episode", {}).get("root_run_id") or "")
        receipt = {
            "status": "deferred",
            "reason_codes": ["deferred_background"],
            "written": 0,
            "root_run_id": root_run_id,
        }
        snapshot["extract_requested"] = True
        snapshot["extract_receipt"] = receipt
        self._store_receipt(session_state, root_run_id, receipt)
        self.meter.record_model_call(
            "extraction",
            status="skipped",
            reason_codes=("deferred_background",),
            attempts=0,
        )

    def _extract_snapshot(self, session_state: Any, snapshot: dict[str, Any]) -> int:
        root_run_id = str(snapshot.get("episode", {}).get("root_run_id") or "")
        existing = self._receipt(session_state, root_run_id)
        if existing is not None and existing.get("status") != "deferred":
            self.meter.record_model_call(
                "extraction",
                status="skipped",
                reason_codes=("already_recorded",),
                attempts=0,
            )
            written = existing.get("written") or 0
            return written if isinstance(written, int) else 0
        outcome = extract_from_snapshot(
            snapshot,
            query=self._side_query,
            directory=self.directory,
            service=self.service,
            policy=self.semantic_policy,
        )
        self._store_receipt(session_state, root_run_id, {
            "status": outcome.status,
            "reason_codes": list(outcome.reason_codes),
            "written": outcome.written,
            "root_run_id": root_run_id,
        })
        self.meter.record_model_call(
            "extraction",
            status=outcome.status,
            reason_codes=outcome.reason_codes,
            attempts=1 if outcome.attempted else 0,
            duration_ms=outcome.duration_ms,
            model=outcome.model,
            usage=outcome.usage,
            written=outcome.written,
        )
        return outcome.written

    def _receipt(self, session_state: Any, root_run_id: str) -> dict[str, Any] | None:
        if not root_run_id:
            return None
        receipts = getattr(session_state, "semantic_extract_receipts", {}) or {}
        item = receipts.get(root_run_id) if isinstance(receipts, dict) else None
        return item if isinstance(item, dict) else None

    def _store_receipt(
        self, session_state: Any, root_run_id: str, receipt: dict[str, Any]
    ) -> None:
        if not root_run_id:
            return
        current = dict(getattr(session_state, "semantic_extract_receipts", {}) or {})
        current[root_run_id] = receipt
        try:
            session_state.semantic_extract_receipts = current
        except Exception:
            logger.debug("memory receipt failed", exc_info=True)

    def _capture(
        self,
        session_state: Any,
        final_answer: str | None,
        termination_reason: str | None,
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        try:
            prior = self._pending_for_root(session_state, _root_run_id(session_state))
            prior_outcome = ""
            if prior is not None:
                prior_outcome = str(prior.get("episode", {}).get("outcome") or "")
            snapshot = snapshot_from_session(
                session_state,
                final_answer,
                termination_reason=termination_reason,
                prior_outcome=prior_outcome,
            )
        except Exception as exc:
            logger.info("episode_capture failure_type=%s", type(exc).__name__)
            return None, False, False
        episode = EpisodeRecord.from_dict(snapshot["episode"])
        decision = self.episode_policy.admission(
            tool_count=len(episode.tools),
            agent_count=len(episode.agents),
            user_texts=tuple(snapshot.get("user_texts") or ()),
            has_delivered_answer=is_delivered_answer(episode.outcome),
        )
        if not decision.keep:
            self._drop_pending(session_state, episode.root_run_id)
            logger.info("episode_skip reason=%s", decision.skip_reason or "skipped")
            return snapshot, False, False
        root_turn_id = str(snapshot.get("root_turn_id") or "")
        return snapshot, live_agent_tasks(session_state, root_turn_id), True

    def _persist_snapshot(
        self, session_state: Any, snapshot: dict[str, Any]
    ) -> EpisodeRecord | None:
        try:
            episode = EpisodeRecord.from_dict(snapshot["episode"])
        except EpisodeStoreError as exc:
            logger.info("episode_persist_failed type=%s", type(exc).__name__)
            self._upsert_pending(session_state, snapshot)
            return None
        decision = self.episode_policy.admission(
            tool_count=len(episode.tools),
            agent_count=len(episode.agents),
            user_texts=tuple(snapshot.get("user_texts") or ()),
            has_delivered_answer=is_delivered_answer(episode.outcome),
        )
        if not decision.keep:
            self._drop_pending(session_state, episode.root_run_id)
            logger.info("episode_skip reason=%s", decision.skip_reason or "skipped")
            return None
        try:
            saved = self.episode_store.save(episode)
        except Exception as exc:
            logger.info("episode_persist_failed type=%s", type(exc).__name__)
            self._upsert_pending(session_state, snapshot)
            return None
        self._drop_pending(session_state, saved.root_run_id)
        return saved

    def _pending_for_turn(self, session_state: Any, root_turn_id: str) -> dict[str, Any] | None:
        for item in getattr(session_state, "pending_episode_finalizes", []):
            if isinstance(item, dict) and item.get("root_turn_id") == root_turn_id:
                return item
        return None

    def _pending_for_root(self, session_state: Any, root_run_id: str) -> dict[str, Any] | None:
        if not root_run_id:
            return None
        for item in getattr(session_state, "pending_episode_finalizes", []):
            episode = item.get("episode") if isinstance(item, dict) else None
            if isinstance(episode, dict) and episode.get("root_run_id") == root_run_id:
                return item
        return None

    def _upsert_pending(self, session_state: Any, snapshot: dict[str, Any]) -> None:
        root_run_id = str(snapshot.get("episode", {}).get("root_run_id") or "")
        kept = [
            item
            for item in getattr(session_state, "pending_episode_finalizes", [])
            if not (
                isinstance(item, dict)
                and isinstance(item.get("episode"), dict)
                and item["episode"].get("root_run_id") == root_run_id
            )
        ]
        kept.append(snapshot)
        session_state.pending_episode_finalizes = kept

    def _drop_pending(self, session_state: Any, root_run_id: str) -> None:
        session_state.pending_episode_finalizes = [
            item
            for item in getattr(session_state, "pending_episode_finalizes", [])
            if not (
                isinstance(item, dict)
                and isinstance(item.get("episode"), dict)
                and item["episode"].get("root_run_id") == root_run_id
            )
        ]

    def _recall_key(self, session_state: Any) -> str:
        turn_id = str(getattr(session_state, "agent_root_turn_id", "") or "")
        if turn_id:
            return turn_id
        return _root_run_id(session_state)


def _project_root_of(session_state: Any) -> Path:
    root = getattr(session_state, "project_root", None) or session_state.workspace_dir
    return Path(root).expanduser().resolve()


def _goal(session_state: Any) -> str:
    reader = getattr(session_state, "current_goal", None)
    if callable(reader):
        return str(reader())
    return ""


def _root_run_id(session_state: Any) -> str:
    run = session_state.active_run() or session_state.current_run()
    if run is None:
        return ""
    return str(run.root_run_id or run.run_id)


__all__ = ["MemoryManager"]
