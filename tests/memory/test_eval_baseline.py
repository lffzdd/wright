"""Offline baseline. It does not call a model and does not score selector quality."""

from wright.application.memory.eval_baseline import evaluate_offline


def test_offline_baseline_reports_lexical_hits_and_known_misses():
    report = evaluate_offline()
    assert report["model_quality"] == "未验证"
    assert report["gate"]["mismatches"] == []
    assert report["missed"] == []
    assert report["candidate_recall"]["total"] > 0
    assert any(item["id"] == "paraphrase-miss" and item["missing"] for item in report["known_misses"])
    cache = next(item for item in report["cases"] if item["id"] == "cache-mechanisms")
    assert "ep-headers" in cache["lexical_distractors"]
    assert "ep-stampede" in cache["lexical_hit"]
    unrelated = next(item for item in report["no_relevant_candidate"] if item["id"] == "unrelated")
    assert unrelated["lexical_ids"] == []
    assert report["sizes"]["selector_input_tokens_max"] <= report["sizes"]["selector_input_budget"]
    assert report["gate"]["skip"] > report["gate"]["extract"]
