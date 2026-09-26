"""Map one finished session turn into an EpisodeRecord. No I/O."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...core.paths import project_id
from ...domain.model.memory import (
    EPISODE_ID_RE,
    EPISODE_SCHEMA_VERSION,
    MAX_EPISODE_OUTCOME_CHARS,
    EpisodeRecord,
    EpisodeStoreError,
    EvidenceRef,
)
from .turn_snapshot import (
    bounded_outcome,
    current_goal,
    flatten_agents,
    real_user_texts,
    root_run_identity,
    terminal_status,
    turn_evidence,
    turn_tools,
    turn_usage,
    turn_verification,
)


def episode_id_for_root_run(root_run_id: str) -> str:
    """Stable id for one root run. Repeating finalize keeps this id."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", root_run_id).strip("-")
    if not safe:
        raise EpisodeStoreError("root_run_id 无法生成 episode id")
    episode_id = f"ep-{safe}"[:184]
    if EPISODE_ID_RE.fullmatch(episode_id) is None:
        raise EpisodeStoreError("episode id 非法")
    return episode_id


def episode_from_session(
    session_state: Any,
    final_answer: str | None,
    *,
    termination_reason: str | None = None,
    prior_outcome: str = "",
) -> EpisodeRecord:
    """Slice the active user turn. Project identity comes from ``project_root``."""
    root_run_id, _active_run_id = root_run_identity(session_state)
    if not root_run_id:
        raise EpisodeStoreError("缺少 root run，不能记录 episode")
    status, reason = terminal_status(
        session_state, termination_reason=termination_reason
    )
    if status not in {"completed", "failed", "cancelled"}:
        raise EpisodeStoreError(f"不能记录未终止的 run status: {status}")
    outcome = bounded_outcome(final_answer)
    if prior_outcome and outcome and prior_outcome not in outcome:
        outcome = f"{prior_outcome}\n{outcome}"[:MAX_EPISODE_OUTCOME_CHARS]
    elif prior_outcome and not outcome:
        outcome = prior_outcome[:MAX_EPISODE_OUTCOME_CHARS]
    project_root = Path(
        getattr(session_state, "project_root", None) or session_state.workspace_dir
    ).expanduser().resolve()
    start = int(getattr(session_state, "active_turn_start_step", 0))
    end = int(getattr(session_state, "step_count", start))
    root_turn_id = str(getattr(session_state, "agent_root_turn_id", "") or "")
    return EpisodeRecord(
        id=episode_id_for_root_run(root_run_id),
        session_id=str(session_state.session_id),
        goal=current_goal(session_state),
        status=status,  # type: ignore[arg-type]
        outcome=outcome,
        started_step=start,
        ended_step=end,
        created_at=datetime.now(timezone.utc).isoformat(),
        plan=session_state.plan_manager.snapshot(),
        tools=turn_tools(session_state),
        agents=flatten_agents(session_state, root_turn_id),
        verification=turn_verification(session_state),
        usage=turn_usage(session_state),
        evidence=tuple(EvidenceRef.from_dict(item) for item in turn_evidence(session_state)),
        version=EPISODE_SCHEMA_VERSION,
        project_id=project_id(project_root),
        project_root=str(project_root),
        root_run_id=root_run_id,
        termination_reason=reason,
    )


def snapshot_from_session(
    session_state: Any,
    final_answer: str | None,
    *,
    termination_reason: str | None = None,
    prior_outcome: str = "",
) -> dict[str, Any]:
    """Frozen facts for this turn, including the user texts used for admission."""
    episode = episode_from_session(
        session_state,
        final_answer,
        termination_reason=termination_reason,
        prior_outcome=prior_outcome,
    )
    return {
        "root_turn_id": str(getattr(session_state, "agent_root_turn_id", "") or ""),
        "user_texts": real_user_texts(session_state),
        "episode": episode.to_dict(),
    }


__all__ = ["episode_from_session", "episode_id_for_root_run", "snapshot_from_session"]
