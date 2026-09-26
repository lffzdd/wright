"""Fixed offline baseline for episode recall and the extraction gate.

Running this module does not call a model. ``evaluate_live`` is separate and
is not imported by the default test run.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from ...domain.model.memory import EpisodeRecord
from ...domain.policy.memory import (
    EpisodePolicy,
    ExtractSignal,
    SemanticExtractPolicy,
    is_delivered_answer,
)
from ...infrastructure.persistence.memory import EpisodeStore
from ...utils.token_counter import estimate_tokens
from .projection import (
    budget_episode_manifest,
    candidate_summary,
    render_episode_for_budget,
)


def _episode(
    episode_id: str,
    goal: str,
    *,
    outcome: str = "",
    status: str = "completed",
    project: str = "alpha",
    tools: tuple[dict, ...] = (),
    verification: tuple[dict, ...] = (),
    created_at: str = "2026-01-01T00:00:00Z",
    total_tokens: int = 2,
) -> EpisodeRecord:
    return EpisodeRecord(
        id=episode_id,
        session_id="eval",
        goal=goal,
        status=status,  # type: ignore[arg-type]
        outcome=outcome,
        started_step=0,
        ended_step=1,
        created_at=created_at,
        plan={},
        tools=tools,
        agents=(),
        verification=verification,
        usage={
            "prompt_tokens": total_tokens,
            "completion_tokens": 0,
            "total_tokens": total_tokens,
        },
        version=3,
        project_id=project,
        project_root=f"/projects/{project}",
        root_run_id=episode_id,
    )


def recall_cases() -> list[dict]:
    """Labeled retrieval cases. A miss stays a miss; labels are not fitted."""
    long_outcome = ("背景说明。" * 200) + "最终改用 bun 而不是 npm。TAILTOKEN_BUN"
    return [
        {
            "id": "zh-login",
            "query": "登录接口返回 401",
            "project": "alpha",
            "relevant": ("ep-zh-login",),
            "irrelevant": ("ep-other-proj",),
            "episodes": (
                _episode("ep-zh-login", "修复登录接口返回 401", outcome="改了 cookie 校验"),
                _episode("ep-other-proj", "修复登录接口返回 401", outcome="另一个项目", project="beta"),
            ),
        },
        {
            "id": "en-identifier",
            "query": "UserSessionStore null",
            "project": "alpha",
            "relevant": ("ep-usersession",),
            "irrelevant": (),
            "episodes": (
                _episode(
                    "ep-usersession",
                    "NullPointerException in UserSessionStore",
                    outcome="guarded the missing session row",
                ),
            ),
        },
        {
            "id": "cache-mechanisms",
            "query": "redis cache stampede after deploy",
            "project": "alpha",
            "relevant": ("ep-stampede",),
            "irrelevant": ("ep-headers",),
            "episodes": (
                _episode("ep-stampede", "redis cache stampede on deploy", outcome="added a single-flight lock"),
                _episode("ep-headers", "browser cache headers too long", outcome="shortened max-age"),
            ),
        },
        {
            "id": "paraphrase-miss",
            "query": "流水线里用例一直等到被掐掉",
            "project": "alpha",
            "relevant": ("ep-hang-en",),
            "irrelevant": (),
            "expect_candidate_miss": True,
            "episodes": (
                _episode(
                    "ep-hang-en",
                    "worker watchdog fired",
                    outcome="raised the fixture timeout and split setup",
                ),
            ),
        },
        {
            "id": "tail-fact",
            "query": "TAILTOKEN_BUN",
            "project": "alpha",
            "relevant": ("ep-tail",),
            "irrelevant": (),
            "summary_must_contain": "TAILTOKEN_BUN",
            "episodes": (
                _episode(
                    "ep-tail",
                    "选择包管理器",
                    outcome=long_outcome,
                    total_tokens=9000001,
                ),
            ),
        },
        {
            "id": "failure-without-answer",
            "query": "ECONNREFUSED postgres",
            "project": "alpha",
            "relevant": ("ep-pg",),
            "irrelevant": (),
            "episodes": (
                _episode(
                    "ep-pg",
                    "连接数据库",
                    outcome="",
                    status="failed",
                    tools=({"name": "execute_command", "error": "ECONNREFUSED postgres", "ok": False, "status": "failed"},),
                ),
            ),
        },
        {
            "id": "unrelated",
            "query": "把按钮改成蓝色",
            "project": "alpha",
            "relevant": (),
            "irrelevant": ("ep-zh-login",),
            "episodes": (
                _episode("ep-unrelated-login", "修复登录接口返回 401", outcome="cookie"),
            ),
        },
        {
            "id": "unverified-success",
            "query": "登录 cookie",
            "project": "alpha",
            "relevant": ("ep-unverified",),
            "irrelevant": (),
            "summary_must_not_contain": "approved=True",
            "episodes": (
                _episode(
                    "ep-unverified",
                    "登录 cookie",
                    outcome="I believe the tests passed",
                    verification=(),
                ),
            ),
        },
        {
            "id": "duplicates",
            "query": "retry the login cookie",
            "project": "alpha",
            "relevant": ("ep-dup-distinct",),
            "irrelevant": (),
            "episodes": (
                _episode("ep-dup-1", "retry the login cookie", outcome="same retry", created_at="2026-01-01T00:00:00Z"),
                _episode("ep-dup-2", "retry the login cookie", outcome="same retry", created_at="2026-01-02T00:00:00Z"),
                _episode("ep-dup-distinct", "retry the login cookie with a new lock", outcome="single-flight", created_at="2026-01-03T00:00:00Z"),
            ),
        },
    ]


def gate_cases() -> list[dict]:
    return [
        {
            "id": "pwd",
            "signals": (
                ExtractSignal("ev-u-1", "user_statement", text="当前目录"),
                ExtractSignal("ev-t-1", "tool_observation", command="pwd", ok=True, execution_status="succeeded"),
            ),
            "should_extract": False,
            "reason": "ephemeral_activity",
            "save_episode": True,
        },
        {
            "id": "remember-bun",
            "signals": (ExtractSignal("ev-u-1", "user_statement", text="记住我以后只用 bun"),),
            "should_extract": True,
            "reason": "user_durable_statement",
            "save_episode": True,
        },
        {
            "id": "many-reads",
            "signals": (
                *(
                    ExtractSignal(
                        f"ev-t-{index}",
                        "tool_observation",
                        command="ls",
                        ok=True,
                        execution_status="succeeded",
                    )
                    for index in range(4)
                ),
                ExtractSignal("ev-u-1", "user_statement", text="看看目录"),
            ),
            "should_extract": False,
            "reason": "ephemeral_activity",
            "save_episode": True,
        },
        {
            "id": "ordinary-edit",
            "signals": (
                ExtractSignal("ev-u-1", "user_statement", text="改一下 README 的标题"),
                ExtractSignal(
                    "ev-t-1",
                    "tool_observation",
                    subject="README.md",
                    ok=True,
                    execution_status="succeeded",
                ),
            ),
            "should_extract": False,
            "reason": "no_durable_signal",
            "save_episode": True,
        },
        {
            "id": "failure-only",
            "signals": (
                ExtractSignal("ev-u-1", "user_statement", text="跑一下测试"),
                ExtractSignal(
                    "ev-t-1",
                    "tool_observation",
                    command="pytest",
                    ok=False,
                    execution_status="failed",
                    error="assertion failed",
                ),
            ),
            "should_extract": False,
            "reason": "failure_without_followup",
            "save_episode": True,
        },
        {
            "id": "greeting",
            "signals": (ExtractSignal("ev-u-1", "user_statement", text="谢谢"),),
            "should_extract": False,
            "reason": "trivial_user_text",
            "save_episode": False,
        },
    ]


def evaluate_offline(root: Path | None = None) -> dict:
    """Score lexical candidates and the extraction gate. This never calls a model.

    ``candidate_recall`` counts BM25 hits only. Recent records can still enter
    the selector pool when the wording does not match; that is reported
    separately and is not treated as lexical recall.
    """
    policy = EpisodePolicy()
    extract_policy = SemanticExtractPolicy()
    temporary = TemporaryDirectory() if root is None else None
    base = Path(temporary.name) if temporary is not None else root
    assert base is not None
    recall_rows = []
    missed = []
    known_misses = []
    no_relevant = []
    selector_tokens = []
    injection_tokens = []
    relevant_total = 0
    relevant_hits = 0
    try:
        for index, case in enumerate(recall_cases()):
            store = EpisodeStore(base / f"case-{index}")
            for episode in case["episodes"]:
                store.save(episode)
            ranked = store.search(
                case["query"],
                scope="current_project",
                project_id=case["project"],
                limit=policy.lexical_candidate_limit,
            )
            lexical_ids = {hit.episode.id for hit in ranked if hit.lexical_score > 0}
            recent = store.recent(
                project_id=case["project"],
                limit=policy.recent_candidate_limit,
            )
            merged = policy.merge_candidates(ranked, recent)
            merged_ids = {hit.episode.id for hit in merged}
            relevant = set(case["relevant"])
            relevant_total += len(relevant)
            lexical_hit = relevant & lexical_ids
            relevant_hits += len(lexical_hit)
            lexical_missing = sorted(relevant - lexical_ids)
            leaked = sorted(set(case["irrelevant"]) & lexical_ids)
            by_episode = {episode.id: episode for episode in case["episodes"]}
            cross_project = [
                item for item in leaked
                if by_episode[item].project_id != case["project"]
            ]
            same_project = [
                item for item in leaked
                if by_episode[item].project_id == case["project"]
            ]
            if cross_project:
                missed.append({
                    "id": case["id"],
                    "missing": [f"leaked:{item}" for item in cross_project],
                })
            if case.get("expect_candidate_miss"):
                known_misses.append({"id": case["id"], "missing": lexical_missing})
                if not lexical_missing:
                    missed.append({"id": case["id"], "missing": ["expected_lexical_miss"]})
            elif lexical_missing:
                missed.append({"id": case["id"], "missing": lexical_missing})
            if not relevant:
                no_relevant.append({
                    "id": case["id"],
                    "lexical_ids": sorted(lexical_ids),
                    "merged_ids": sorted(merged_ids),
                })
            manifest, _dropped = budget_episode_manifest(
                [hit.episode for hit in merged],
                token_budget=policy.selector_input_token_budget,
            )
            selector_tokens.append(estimate_tokens(manifest))
            for episode in case["episodes"]:
                if episode.project_id != case["project"]:
                    continue
                line = candidate_summary(episode, char_budget=700)
                required = case.get("summary_must_contain")
                if required and episode.id in relevant and required not in line:
                    missed.append({"id": case["id"], "missing": [f"summary:{required}"]})
                banned = case.get("summary_must_not_contain")
                if banned and banned in line:
                    missed.append({"id": case["id"], "missing": [f"banned:{banned}"]})
                if "9000001" in line:
                    missed.append({"id": case["id"], "missing": ["usage_leaked"]})
                rendered = render_episode_for_budget(episode, token_limit=400)
                injection_tokens.append(estimate_tokens(rendered or ""))
            recall_rows.append({
                "id": case["id"],
                "lexical_ids": sorted(lexical_ids),
                "merged_ids": sorted(merged_ids),
                "lexical_hit": sorted(lexical_hit),
                "lexical_distractors": same_project,
                "selector_tokens": selector_tokens[-1],
            })
        gate_rows = []
        mismatches = []
        extract_count = 0
        skip_count = 0
        for case in gate_cases():
            decision = extract_policy.decide(case["signals"])
            admission = policy.admission(
                tool_count=sum(1 for item in case["signals"] if item.kind == "tool_observation"),
                agent_count=0,
                user_texts=tuple(
                    item.text for item in case["signals"] if item.kind == "user_statement"
                ),
                has_delivered_answer=is_delivered_answer("done" if case["save_episode"] else ""),
            )
            if decision.should_extract:
                extract_count += 1
            else:
                skip_count += 1
            if decision.should_extract != case["should_extract"] or (
                not decision.should_extract and case["reason"] not in decision.reason_codes
            ):
                mismatches.append({
                    "id": case["id"],
                    "expected": case["should_extract"],
                    "actual": decision.should_extract,
                    "reasons": list(decision.reason_codes),
                })
            if admission.keep != case["save_episode"]:
                mismatches.append({
                    "id": case["id"],
                    "expected_save": case["save_episode"],
                    "actual_save": admission.keep,
                    "skip_reason": admission.skip_reason,
                })
            gate_rows.append({
                "id": case["id"],
                "should_extract": decision.should_extract,
                "reason_codes": list(decision.reason_codes),
                "save_episode": admission.keep,
            })
        return {
            "candidate_recall": {
                "hits": relevant_hits,
                "total": relevant_total,
                "rate": (relevant_hits / relevant_total) if relevant_total else None,
                "note": "BM25 only. Recent-pool inclusion is not counted as a hit.",
            },
            "cases": recall_rows,
            "missed": missed,
            "known_misses": known_misses,
            "no_relevant_candidate": no_relevant,
            "gate": {
                "extract": extract_count,
                "skip": skip_count,
                "mismatches": mismatches,
                "cases": gate_rows,
            },
            "sizes": {
                "selector_input_tokens_max": max(selector_tokens) if selector_tokens else 0,
                "selector_input_budget": policy.selector_input_token_budget,
                "injection_tokens_max": max(injection_tokens) if injection_tokens else 0,
            },
            "model_quality": "未验证",
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


__all__ = ["evaluate_offline", "gate_cases", "recall_cases"]
