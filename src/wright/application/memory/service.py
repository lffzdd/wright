"""MemoryService: every read and write of long-term memory goes through here.

Tools, the extractor, consolidation, and the web console all call this class.
It owns the write pipeline (docs/memory.md, "写入流水线"), the change log, and a
generation counter that tells cached recall when memory has changed.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ...core.logger import get_logger
from ...domain.gateway.memory import (
    IContextSelector,
    IEpisodeEvidenceSource,
    IMemoryStore,
)
from ...domain.model.memory import (
    FACT_KINDS,
    FACT_SUBJECTS,
    GLOBAL_SPACE_ID,
    MAX_COMMITMENT_CHARS,
    MAX_FACT_CHARS,
    MAX_WHY_CHARS,
    SENSITIVITIES,
    TIERS,
    USER_ORIGINS,
    ChangeRecord,
    Commitment,
    CommitmentError,
    EpisodeRecord,
    Fact,
    Space,
    SpaceError,
    episode_fact_pieces,
    is_locator,
    normalize_due,
    parse_locator,
    personal_space_id,
    repo_space_id,
    space_type_of,
)
from ...domain.policy.memory import (
    DUPLICATE_SIMILARITY,
    RELATED_SIMILARITY,
    EpisodePolicy,
    bm25_scores,
    classify_sensitivity,
    commitment_lifecycle,
    default_tier,
    episode_should_compact,
    extract_markers,
    fact_lifecycle,
    find_secrets,
    infer_origin,
    normalize_for_match,
    normalize_markers,
    similarity,
    tokenize,
)
from .dto import SearchHit, WriteOutcome, WriteRequest

logger = get_logger(__name__)

CONSOLIDATION_INTERVAL_HOURS = 20


class MemoryServiceError(ValueError):
    """A request the service refuses. The message is safe to show the model."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class MemoryService:
    def __init__(
        self,
        store: IMemoryStore,
        *,
        selector: IContextSelector | None = None,
        evidence_source: IEpisodeEvidenceSource | None = None,
        episode_policy: EpisodePolicy | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.selector = selector
        self.evidence_source = evidence_source
        self.episode_policy = episode_policy or EpisodePolicy()
        self.clock = clock
        self._generation = 0
        self._lock = threading.Lock()
        self.store.upsert_space(Space(GLOBAL_SPACE_ID, "global", "global", self._stamp()))

    # bookkeeping -------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    def _bump(self) -> None:
        with self._lock:
            self._generation += 1

    def _stamp(self) -> str:
        return _iso(self.clock())

    def _log(
        self,
        actor: str,
        action: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        note: str = "",
        scope: str = "",
    ) -> ChangeRecord:
        return self.store.append_change(
            at=self._stamp(), actor=actor, action=action, target_id=target_id,
            before=before, after=after, note=note, scope=scope or _scope_of(after) or _scope_of(before),
        )

    # spaces ------------------------------------------------------------

    def ensure_repo_space(self, project_id: str, project_root: str = "") -> Space:
        space_id = repo_space_id(project_id)
        name = project_root.rstrip("/").rsplit("/", 1)[-1] if project_root else project_id
        return self.store.upsert_space(Space(space_id, "repo", name or project_id, self._stamp(), project_root))

    def ensure_personal_space(self, name: str) -> Space:
        space_id = personal_space_id(name)
        return self.store.upsert_space(Space(space_id, "personal", str(name).strip()[:60], self._stamp()))

    def get_space(self, space_id: str) -> Space | None:
        return self.store.get_space(space_id)

    def list_spaces(self) -> list[Space]:
        return self.store.list_spaces()

    def resolve_space(self, value: str, *, current: str, allow_create: bool = False) -> str:
        """Turn a tool argument into a space id: "current", "global", "repo", an id, or a personal name."""
        text = str(value or "").strip()
        if not text or text in {"current", "default"}:
            return current
        if text == "global":
            return GLOBAL_SPACE_ID
        if text in {"repo", "project"}:
            repo = next((item for item in (current,) if item.startswith("repo:")), "")
            if not repo:
                raise MemoryServiceError("no project is bound to this session")
            return repo
        if text.startswith(("repo:", "personal:")):
            if self.store.get_space(text) is None:
                if text.startswith("personal:") and allow_create:
                    return self.ensure_personal_space(text.split(":", 1)[1]).id
                raise MemoryServiceError(f"unknown space: {text}")
            return text
        space_id = personal_space_id(text)
        if self.store.get_space(space_id) is None:
            if not allow_create:
                raise MemoryServiceError(f"unknown space: {text}")
            self.ensure_personal_space(text)
        return space_id

    # facts: write pipeline --------------------------------------------

    def write_fact(self, request: WriteRequest) -> WriteOutcome:
        content = " ".join(str(request.content or "").split()) if "\n" not in str(request.content or "") else str(request.content).strip()
        why = str(request.why or "").strip()
        if not content:
            return WriteOutcome("rejected", message="content is empty")
        if len(content) > MAX_FACT_CHARS:
            return WriteOutcome("rejected", message=f"content is longer than {MAX_FACT_CHARS} characters; split it")
        if len(why) > MAX_WHY_CHARS:
            why = why[:MAX_WHY_CHARS]
        if request.kind not in FACT_KINDS:
            return WriteOutcome("rejected", message=f"kind must be one of {', '.join(FACT_KINDS)}")
        subject = request.subject if request.subject in FACT_SUBJECTS else "other"
        if request.sensitivity_hint and request.sensitivity_hint not in SENSITIVITIES:
            return WriteOutcome("rejected", message="invalid sensitivity")

        # 1. credentials never enter memory
        secrets = find_secrets("\n".join([content, why, *request.markers]))
        if secrets:
            return WriteOutcome("rejected", message=f"looks like a credential ({secrets[0][0]}); credentials are never stored")

        # 2. where it applies
        space_id = request.space_id or GLOBAL_SPACE_ID
        try:
            space_type = space_type_of(space_id)
        except SpaceError as exc:
            return WriteOutcome("rejected", message=str(exc))
        if self.store.get_space(space_id) is None:
            return WriteOutcome("rejected", message=f"unknown space: {space_id}")

        # 3. where it came from
        if request.actor == "user":
            origin = "user_stated"
        elif request.actor == "extractor":
            origin = "agent_inferred"
        else:
            origin = infer_origin(content, user_texts=request.user_texts, external_texts=request.external_texts)

        # 4. how private it is
        sensitivity = classify_sensitivity(
            content,
            subject=subject,  # type: ignore[arg-type]
            space_type=space_type,
            hint=request.sensitivity_hint or None,  # type: ignore[arg-type]
        )
        if sensitivity == "sensitive" and origin not in USER_ORIGINS:
            return WriteOutcome(
                "needs_user",
                message=(
                    "this looks like sensitive personal information (health, money, relationships, beliefs, "
                    "legal or identity documents). It is only stored when the user explicitly asks; ask them first"
                ),
            )
        moved_to = ""
        if sensitivity == "sensitive" and space_type == "repo":
            space_id, moved_to = GLOBAL_SPACE_ID, GLOBAL_SPACE_ID

        # 5. lifecycle defaults
        status = "quarantined" if origin == "external" else "active"
        tier = "archived" if origin == "external" else default_tier(origin, pin=request.pin)  # type: ignore[arg-type]
        markers = normalize_markers([*request.markers, *extract_markers(content)])

        # 6. duplicates and replacement
        superseded: Fact | None = None
        if request.supersedes:
            superseded = self.store.get_fact(request.supersedes)
            if superseded is None or superseded.status != "active":
                return WriteOutcome("rejected", message=f"supersedes: {request.supersedes} is not an active fact")
        candidates = self.store.list_facts(space_ids=[space_id], statuses=["active", "quarantined"])
        scored = sorted(
            ((similarity(content, item.content), item) for item in candidates if item.id != request.supersedes),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if scored and not request.force_new and not superseded and scored[0][0] >= DUPLICATE_SIMILARITY:
            existing = scored[0][1]
            return WriteOutcome(
                "duplicate",
                fact=existing,
                message=f"{existing.id} already says this; update it with memory_update instead of writing again",
            )
        related = tuple(item for score, item in scored[:3] if score >= RELATED_SIMILARITY)

        now = self._stamp()
        fact = Fact(
            id=f"f-{uuid.uuid4().hex[:16]}",
            space_id=space_id,
            kind=request.kind,  # type: ignore[arg-type]
            subject=subject,  # type: ignore[arg-type]
            content=content,
            why=why,
            origin=origin,  # type: ignore[arg-type]
            sensitivity=sensitivity,
            tier=tier,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            markers=tuple(markers),
            sources=tuple(dict.fromkeys(item for item in request.sources if item)),
            created_at=now,
            updated_at=now,
            valid_from=now,
        )
        with self.store.transaction():
            self.store.insert_fact(fact)
            self._log(request.actor, "create", fact.id, None, fact.to_dict())
            if superseded is not None:
                old = superseded.evolve(
                    status="superseded", superseded_by=fact.id, valid_to=now,
                    updated_at=now, revision=superseded.revision + 1,
                )
                self.store.replace_fact(old, expected_revision=superseded.revision)
                self._log(request.actor, "supersede", old.id, superseded.to_dict(), old.to_dict(), f"by {fact.id}")
        self._bump()
        if status == "quarantined":
            message = (
                f"{fact.id} came from tool output, not from the user, so it is quarantined: it is not recalled until "
                "the user confirms it in the memory console. Unconfirmed items are deleted after 7 days"
            )
            return WriteOutcome("quarantined", fact=fact, message=message, related=related)
        notes = [f"saved {fact.id} ({fact.tier}, {fact.origin}, sensitivity={fact.sensitivity})"]
        if moved_to:
            notes.append("sensitive personal information is kept in the global space, not in a project")
        if related:
            notes.append("similar facts exist; if one is now wrong, pass its id as supersedes next time")
        return WriteOutcome(
            "created", fact=fact, message="; ".join(notes), related=related,
            superseded=superseded.id if superseded else "", moved_to=moved_to,
        )

    def update_fact(
        self,
        fact_id: str,
        *,
        actor: str = "agent",
        expected_revision: int | None = None,
        content: str | None = None,
        why: str | None = None,
        kind: str | None = None,
        tier: str | None = None,
        markers: Sequence[str] | None = None,
        shareable: bool | None = None,
        user_texts: Sequence[str] = (),
        external_texts: Sequence[str] = (),
    ) -> Fact:
        current = self.store.get_fact(fact_id)
        if current is None or current.status == "deleted":
            raise MemoryServiceError(f"no such fact: {fact_id}")
        if current.status == "superseded":
            raise MemoryServiceError(f"{fact_id} was superseded by {current.superseded_by}; update that one")
        changes: dict[str, Any] = {}
        if content is not None:
            text = str(content).strip()
            if not text:
                raise MemoryServiceError("content is empty; use memory_forget to remove a fact")
            if len(text) > MAX_FACT_CHARS:
                raise MemoryServiceError(f"content is longer than {MAX_FACT_CHARS} characters")
            if find_secrets(text):
                raise MemoryServiceError("looks like a credential; credentials are never stored")
            changes["content"] = text
            origin = current.origin
            if actor == "user":
                origin = "user_stated"
            elif actor == "agent":
                inferred = infer_origin(text, user_texts=user_texts, external_texts=external_texts)
                if inferred == "external":
                    raise MemoryServiceError(
                        "the new text comes from tool output, not from the user; write it as a new fact so it is quarantined"
                    )
                if inferred == "agent_inferred" and current.origin in USER_ORIGINS:
                    origin = current.origin
                else:
                    origin = inferred
            sensitivity = classify_sensitivity(
                text, subject=current.subject, space_type=space_type_of(current.space_id), hint=current.sensitivity,
            )
            if sensitivity == "sensitive" and origin not in USER_ORIGINS:
                raise MemoryServiceError("sensitive personal information is only stored when the user asks; ask them first")
            changes["origin"] = origin
            changes["sensitivity"] = sensitivity
            changes["markers"] = tuple(normalize_markers([*current.markers, *extract_markers(text)]))
        if why is not None:
            changes["why"] = str(why).strip()[:MAX_WHY_CHARS]
        if kind is not None:
            if kind not in FACT_KINDS:
                raise MemoryServiceError(f"kind must be one of {', '.join(FACT_KINDS)}")
            changes["kind"] = kind
        if tier is not None:
            if tier not in TIERS:
                raise MemoryServiceError("tier must be pinned, indexed or archived")
            if tier == "pinned" and changes.get("origin", current.origin) not in USER_ORIGINS:
                raise MemoryServiceError("only facts the user stated can be pinned")
            changes["tier"] = tier
        if markers is not None:
            changes["markers"] = tuple(normalize_markers([*markers, *extract_markers(changes.get("content", current.content))]))
        if shareable is not None:
            if actor != "user":
                raise MemoryServiceError("only the user can mark a fact shareable")
            changes["shareable"] = bool(shareable)
        if not changes:
            return current
        updated = current.evolve(**changes, updated_at=self._stamp(), revision=current.revision + 1)
        with self.store.transaction():
            self.store.replace_fact(updated, expected_revision=expected_revision if expected_revision is not None else current.revision)
            self._log(actor, "update", fact_id, current.to_dict(), updated.to_dict())
        self._bump()
        return updated

    def confirm_fact(self, fact_id: str) -> Fact:
        """User action: accept a quarantined fact as their own."""
        current = self.store.get_fact(fact_id)
        if current is None or current.status != "quarantined":
            raise MemoryServiceError(f"{fact_id} is not waiting for confirmation")
        updated = current.evolve(
            status="active", origin="user_confirmed", tier="indexed",
            updated_at=self._stamp(), revision=current.revision + 1,
        )
        with self.store.transaction():
            self.store.replace_fact(updated, expected_revision=current.revision)
            self._log("user", "confirm", fact_id, current.to_dict(), updated.to_dict())
        self._bump()
        return updated

    def get_fact(self, fact_id: str) -> Fact | None:
        return self.store.get_fact(fact_id)

    def list_facts(self, *, space_ids: Sequence[str] | None = None, statuses: Sequence[str] | None = ("active",)) -> list[Fact]:
        return self.store.list_facts(space_ids=space_ids, statuses=statuses)

    def protected_facts(self) -> list[Fact]:
        """Every fact the egress check guards, across all spaces."""
        return self.store.list_facts(space_ids=None, statuses=["active", "superseded"])

    def touch(self, fact_ids: Sequence[str]) -> None:
        try:
            self.store.touch_facts(list(fact_ids), self._stamp())
        except Exception:
            logger.debug("memory touch failed", exc_info=True)

    # forgetting --------------------------------------------------------

    def forget(self, target_id: str, *, actor: str = "agent", reason: str = "") -> list[str]:
        """Remove a fact, episode or commitment, plus inferred facts that only came from it.

        Forgetting is real: the content is not kept in the change log.
        """
        removed: list[str] = []
        with self.store.transaction():
            if target_id.startswith("f-"):
                fact = self.store.get_fact(target_id)
                if fact is None or fact.status == "deleted":
                    raise MemoryServiceError(f"no such fact: {target_id}")
                scope = fact.space_id
                self.store.purge_fact(target_id)
            elif target_id.startswith("ep-"):
                episode = self.store.get_episode(target_id)
                if episode is None or not self.store.delete_episode(target_id):
                    raise MemoryServiceError(f"no such episode: {target_id}")
                scope = repo_space_id(episode.project_id) if episode.project_id else ""
            elif target_id.startswith("c-"):
                commitment = self.store.get_commitment(target_id)
                if commitment is None or not self.store.delete_commitment(target_id):
                    raise MemoryServiceError(f"no such commitment: {target_id}")
                scope = commitment.space_id
            else:
                raise MemoryServiceError(f"unknown memory id: {target_id}")
            removed.append(target_id)
            # Earlier snapshots hold the content too; the rows stay, the content goes.
            self.store.scrub_changes(target_id)
            self._log(actor, "forget", target_id, None, None, reason[:200], scope=scope)
            for dependent in self._dependents(target_id):
                self.store.purge_fact(dependent.id)
                self.store.scrub_changes(dependent.id)
                removed.append(dependent.id)
                self._log(actor, "forget", dependent.id, None, None, f"derived from {target_id}", scope=dependent.space_id)
        self._bump()
        return removed

    def _dependents(self, target_id: str) -> list[Fact]:
        """Inferred facts whose every source points at ``target_id``."""
        found: list[Fact] = []
        for fact in self.store.list_facts(statuses=["active", "superseded", "quarantined"]):
            if fact.origin != "agent_inferred" or not fact.sources:
                continue
            if all(_source_points_at(source, target_id) for source in fact.sources):
                found.append(fact)
        return found

    # episodes ----------------------------------------------------------

    def save_episode(self, episode: EpisodeRecord) -> EpisodeRecord:
        if episode.project_id:
            self.ensure_repo_space(episode.project_id, episode.project_root)
        saved = self.store.save_episode(episode)
        self._bump()
        return saved

    def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        return self.store.get_episode(episode_id)

    def list_episodes(self, *, project_ids: Sequence[str] | None = None, limit: int | None = None) -> list[EpisodeRecord]:
        return self.store.list_episodes(project_ids=project_ids, limit=limit)

    def read_evidence(self, episode: EpisodeRecord) -> list[Any]:
        if self.evidence_source is None or not episode.evidence:
            return []
        try:
            return list(self.evidence_source.load(episode.evidence))
        except Exception as exc:
            logger.info("episode_evidence failure_type=%s", type(exc).__name__)
            return []

    # commitments -------------------------------------------------------

    def create_commitment(self, content: str, *, space_id: str, due: str = "", actor: str = "agent", source_episode: str = "") -> Commitment:
        text = " ".join(str(content or "").split())
        if not text:
            raise MemoryServiceError("content is empty")
        if len(text) > MAX_COMMITMENT_CHARS:
            raise MemoryServiceError(f"content is longer than {MAX_COMMITMENT_CHARS} characters")
        if find_secrets(text):
            raise MemoryServiceError("looks like a credential; credentials are never stored")
        if self.store.get_space(space_id) is None:
            raise MemoryServiceError(f"unknown space: {space_id}")
        try:
            due_value = normalize_due(due)
        except CommitmentError as exc:
            raise MemoryServiceError(str(exc)) from exc
        now = self._stamp()
        item = Commitment(f"c-{uuid.uuid4().hex[:16]}", space_id, text, "open", now, now, due_value, source_episode)
        with self.store.transaction():
            self.store.save_commitment(item)
            self._log(actor, "create", item.id, None, item.to_dict())
        self._bump()
        return item

    def update_commitment(self, commitment_id: str, *, actor: str = "agent", status: str | None = None, due: str | None = None, content: str | None = None) -> Commitment:
        current = self.store.get_commitment(commitment_id)
        if current is None:
            raise MemoryServiceError(f"no such commitment: {commitment_id}")
        changes: dict[str, Any] = {}
        if status is not None:
            if status not in {"open", "done", "dismissed"}:
                raise MemoryServiceError("status must be open, done or dismissed")
            changes["status"] = status
        if due is not None:
            try:
                changes["due"] = normalize_due(due)
            except CommitmentError as exc:
                raise MemoryServiceError(str(exc)) from exc
        if content is not None:
            text = " ".join(str(content).split())
            if not text or find_secrets(text):
                raise MemoryServiceError("content is empty or looks like a credential")
            changes["content"] = text[:MAX_COMMITMENT_CHARS]
        if not changes:
            return current
        updated = current.evolve(**changes, updated_at=self._stamp())
        with self.store.transaction():
            self.store.save_commitment(updated)
            self._log(actor, "update", commitment_id, current.to_dict(), updated.to_dict())
        self._bump()
        return updated

    def list_commitments(self, *, space_ids: Sequence[str] | None = None, statuses: Sequence[str] | None = None) -> list[Commitment]:
        return self.store.list_commitments(space_ids=space_ids, statuses=statuses)

    # search and read ---------------------------------------------------

    def search(
        self,
        query: str,
        *,
        space_ids: Sequence[str],
        types: Sequence[str] = ("fact", "episode", "commitment"),
        limit: int = 10,
        include_inactive: bool = False,
    ) -> list[SearchHit]:
        space_set = list(dict.fromkeys(space_ids))
        documents: list[tuple[SearchHit, str]] = []
        if "fact" in types:
            statuses = ["active", "superseded", "quarantined"] if include_inactive else ["active"]
            for fact in self.store.list_facts(space_ids=space_set, statuses=statuses):
                hit = SearchHit(fact.id, "fact", fact.space_id, fact.summary, 0.0, fact.created_at, fact.status)
                documents.append((hit, " ".join([fact.content, fact.why, *fact.markers, fact.kind])))
        if "episode" in types:
            project_ids = [item.split(":", 1)[1] for item in space_set if item.startswith("repo:")]
            for episode in self.store.list_episodes(project_ids=project_ids):
                pieces = episode_fact_pieces(episode)
                summary = f"{episode.created_at[:10]} {episode.status}: {' '.join(episode.goal.split())[:110]}"
                text = " ".join([episode.goal, episode.goal, episode.outcome, pieces["errors"], pieces["objects"], pieces["names"]])
                documents.append((SearchHit(episode.id, "episode", episode.space_id, summary, 0.0, episode.created_at, episode.status), text))
        if "commitment" in types:
            statuses = None if include_inactive else ["open"]
            for item in self.store.list_commitments(space_ids=space_set, statuses=statuses):
                due = f" (due {item.due})" if item.due else ""
                documents.append((SearchHit(item.id, "commitment", item.space_id, item.content[:110] + due, 0.0, item.created_at, item.status), item.content))
        if not documents:
            return []
        terms = tokenize(query or "")
        if not terms:
            ranked = sorted(documents, key=lambda pair: pair[0].created_at, reverse=True)
            return [hit for hit, _text in ranked[:limit]]
        scores = bm25_scores([tokenize(text) for _hit, text in documents], terms)
        ranked_scores = sorted(
            ((score, hit) for score, (hit, _text) in zip(scores, documents, strict=True) if score > 0),
            key=lambda pair: (pair[0], pair[1].created_at),
            reverse=True,
        )
        return [replace(hit, score=score) for score, hit in ranked_scores[:limit]]

    # change log --------------------------------------------------------

    def changes(
        self, *, limit: int = 100, target_id: str = "", scopes: Sequence[str] | None = None
    ) -> list[ChangeRecord]:
        return self.store.list_changes(limit=limit, target_id=target_id, scopes=scopes)

    def get_change(self, change_id: int) -> ChangeRecord | None:
        return self.store.get_change(change_id)

    def revert(self, change_id: int, *, actor: str = "user") -> ChangeRecord:
        change = self.store.get_change(change_id)
        if change is None:
            raise MemoryServiceError(f"no such change: {change_id}")
        if change.reverted:
            raise MemoryServiceError(f"change {change_id} was already reverted")
        if change.action == "forget":
            raise MemoryServiceError("a forget cannot be undone; the content was not kept")
        if change.before is None and change.after is None:
            raise MemoryServiceError("this record was forgotten; its content was not kept")
        target = change.target_id
        with self.store.transaction():
            if target.startswith("f-"):
                current = self.store.get_fact(target)
                if change.before is None:
                    if current is not None:
                        self.store.purge_fact(target)
                else:
                    restored = Fact.from_dict(change.before)
                    if current is None:
                        self.store.insert_fact(restored.evolve(revision=restored.revision + 1))
                    else:
                        self.store.replace_fact(restored.evolve(revision=current.revision + 1))
            elif target.startswith("ep-") and change.before is not None:
                restored_episode = EpisodeRecord.from_dict(change.before)
                if self.store.get_episode(target) is None:
                    self.store.save_episode(restored_episode)
                else:
                    self.store.replace_episode(restored_episode)
            elif target.startswith("c-"):
                if change.before is None:
                    self.store.delete_commitment(target)
                else:
                    self.store.save_commitment(Commitment.from_dict(change.before))
            else:
                raise MemoryServiceError("this change cannot be reverted")
            self.store.mark_reverted(change_id)
            record = self._log(actor, "revert", target, change.after, change.before, f"revert #{change_id}")
        self._bump()
        return record

    # consolidation -----------------------------------------------------

    def consolidate_if_due(self) -> list[ChangeRecord]:
        last = self.store.get_meta("last_consolidated_at") or ""
        now = self.clock()
        if last:
            try:
                previous = datetime.fromisoformat(last)
                if (now - previous).total_seconds() < CONSOLIDATION_INTERVAL_HOURS * 3600:
                    return []
            except ValueError:
                pass
        return self.consolidate()

    def consolidate(self) -> list[ChangeRecord]:
        """Tier changes, expiry, exact-duplicate merges, episode compaction, commitment expiry."""
        now = self.clock()
        stamp = _iso(now)
        changes: list[ChangeRecord] = []
        with self.store.transaction():
            facts = self.store.list_facts(statuses=["active", "quarantined"])
            for fact in facts:
                action = fact_lifecycle(fact, now)
                if action is None:
                    continue
                if action.action == "delete":
                    self.store.purge_fact(fact.id)
                    changes.append(self._log("consolidation", "delete", fact.id, fact.to_dict(), None, action.reason))
                    continue
                tier = "indexed" if action.action == "promote" else "archived"
                updated = fact.evolve(tier=tier, updated_at=stamp, revision=fact.revision + 1)
                self.store.replace_fact(updated)
                changes.append(self._log("consolidation", action.action, fact.id, fact.to_dict(), updated.to_dict(), action.reason))
            changes.extend(self._merge_exact_duplicates(stamp))
            for episode in self.store.list_episodes():
                if not episode_should_compact(episode, now):
                    continue
                compacted = replace(
                    episode, tools=(), agents=(), evidence=(), plan={}, verification=(),
                    outcome=episode.outcome[:600], compacted=True,
                )
                self.store.replace_episode(compacted)
                changes.append(self._log("consolidation", "compact", episode.id, episode.to_dict(), compacted.to_dict(), "older than 30 days"))
            for item in self.store.list_commitments():
                step = commitment_lifecycle(item, now)
                if step == "expire":
                    updated_item = item.evolve(status="expired", updated_at=stamp)
                    self.store.save_commitment(updated_item)
                    changes.append(self._log("consolidation", "expire", item.id, item.to_dict(), updated_item.to_dict(), "overdue"))
                elif step == "delete":
                    self.store.delete_commitment(item.id)
                    changes.append(self._log("consolidation", "delete", item.id, item.to_dict(), None, "closed long ago"))
            self.store.set_meta("last_consolidated_at", stamp)
        if changes:
            self._bump()
        return changes

    def _merge_exact_duplicates(self, stamp: str) -> list[ChangeRecord]:
        changes: list[ChangeRecord] = []
        seen: dict[tuple[str, str], Fact] = {}
        for fact in sorted(self.store.list_facts(statuses=["active"]), key=lambda item: item.created_at):
            key = (fact.space_id, normalize_for_match(fact.content))
            keeper = seen.get(key)
            if keeper is None:
                seen[key] = fact
                continue
            merged = fact.evolve(status="superseded", superseded_by=keeper.id, valid_to=stamp, updated_at=stamp, revision=fact.revision + 1)
            self.store.replace_fact(merged)
            changes.append(self._log("consolidation", "merge", fact.id, fact.to_dict(), merged.to_dict(), f"same as {keeper.id}"))
        return changes


def _source_points_at(source: str, target_id: str) -> bool:
    if source == target_id:
        return True
    if is_locator(source):
        try:
            return parse_locator(source).episode_id == target_id
        except Exception:
            return False
    return False


def _scope_of(snapshot: dict[str, Any] | None) -> str:
    """The space a fact, commitment or episode snapshot belongs to."""
    if not snapshot:
        return ""
    space = snapshot.get("space_id")
    if isinstance(space, str) and space:
        return space
    project = snapshot.get("project_id")
    return repo_space_id(project) if isinstance(project, str) and project else ""


__all__ = ["CONSOLIDATION_INTERVAL_HOURS", "MemoryService", "MemoryServiceError"]

