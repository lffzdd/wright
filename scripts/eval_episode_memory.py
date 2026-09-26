"""Offline episode-memory baseline.

Default mode does not call a model and does not read API keys.

    uv run python scripts/eval_episode_memory.py

``--live`` asks the configured selector to choose among the labeled
candidates. Lexical recall stays in the offline section. Model quality is
``未验证`` unless this flag is actually used. The script uses
``OPENAI_MEMORY_MODEL`` when set, otherwise ``OPENAI_MODEL``, with the same
base URL and API key as the app. Credentials and prompts are not printed.

    uv run python scripts/eval_episode_memory.py --live
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Episode memory baseline")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the configured memory model. Default is offline and unverified.",
    )
    args = parser.parse_args()
    from wright.application.memory.eval_baseline import evaluate_offline

    report = evaluate_offline()
    report["model_quality"] = "未验证"
    if args.live:
        report["model_quality"] = _live()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["missed"] or report["gate"]["mismatches"]:
        return 1
    if args.live and report["model_quality"].get("status") != "ran":
        return 2
    return 0


def _live() -> dict:
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MEMORY_MODEL") or os.getenv("OPENAI_MODEL")
    if not base_url or not api_key or not model:
        return {
            "status": "未验证",
            "reason": "missing OPENAI_BASE_URL, OPENAI_API_KEY, or OPENAI_MODEL",
        }
    from wright.application.memory.eval_baseline import recall_cases
    from wright.application.memory.llm_util import metered_events
    from wright.application.memory.projection import candidate_summary
    from wright.infrastructure.llm.llm import LLMClient
    from wright.infrastructure.persistence.memory.selector import LlmContextSelector

    client = LLMClient(base_url=base_url, api_key=api_key, model=model, stream=False)
    usages = []

    def query(messages, **kwargs):
        def observe(usage):
            usages.append(usage)

        yield from metered_events(client(messages, **kwargs), observe)

    selector = LlmContextSelector(query)
    rows = []
    for case in recall_cases():
        lines = [
            candidate_summary(episode)
            for episode in case["episodes"]
            if episode.project_id == case["project"]
        ]
        choice = selector.select(
            task=case["query"],
            semantic_manifest="",
            episode_manifest="\n".join(lines),
        )
        rows.append({
            "id": case["id"],
            "selected": list(choice.episode_ids),
            "failed": choice.failed,
            "failure_type": choice.failure_type,
            "relevant": list(case["relevant"]),
        })
    last = usages[-1] if usages else None
    return {
        "status": "ran",
        "model": model,
        "calls": len(rows),
        "usage_available": last is not None,
        "last_total_tokens": None if last is None else last.total_tokens,
        "cases": rows,
        "note": "Selector quality on labeled candidates. Not a bill and not lexical recall.",
    }


if __name__ == "__main__":
    sys.exit(main())
