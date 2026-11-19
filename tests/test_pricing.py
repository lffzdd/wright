import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from wright.application.composition.runtime import _price_book
from wright.domain.model.llm import UsageEvent, UsageRecord
from wright.domain.model.llm.pricing import (
    ModelPrice,
    PriceBook,
    PriceError,
    parse_price_entry,
)
from wright.domain.model.llm.provider_usage import usage_from_provider
from wright.domain.model.session import Session
from wright.infrastructure.llm.llm import LLMClient
from wright.infrastructure.persistence.session.repository import FileSessionRepository

# Wednesday 2026-11-18: 02:00 UTC is DeepSeek peak time, 12:00 UTC is not.
PEAK = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
OFF_PEAK = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


def test_list_price_cost_bills_cached_tokens_at_the_cache_rate():
    book = PriceBook()
    # 1M prompt tokens, 400k of them cached, 100k output on Claude Opus 5.5 ($4 / $20, cache $0.20).
    cost = book.cost("claude-opus-5-5", 1_000_000, 100_000, 400_000, at=PEAK)
    assert cost == pytest.approx(0.6 * 4 + 0.4 * 0.20 + 0.1 * 20)


def test_model_names_from_gateways_and_dated_snapshots_find_their_family():
    book = PriceBook()
    assert book.price_for("openai/gpt-5") == book.price_for("gpt-5")
    assert book.price_for("gpt-5-2025-08-07") == book.price_for("gpt-5")
    assert book.price_for("Claude-Haiku-4-5-20251001") == book.price_for("claude-haiku-4-5")
    # A longer name is a different model, not a snapshot of a shorter one.
    assert book.price_for("gpt-5-mini") != book.price_for("gpt-5")
    assert book.price_for("my-local-model") is None
    assert book.cost("my-local-model", 10, 10) is None


def test_deepseek_off_peak_hours_cost_half():
    book = PriceBook()
    peak = book.cost("deepseek-v4-flash", 1_000_000, 1_000_000, at=PEAK)
    assert peak == pytest.approx(0.30 + 1.20)
    assert book.cost("deepseek-v4-flash", 1_000_000, 1_000_000, at=OFF_PEAK) == pytest.approx(peak / 2)
    weekend = datetime(2026, 11, 21, 2, tzinfo=timezone.utc)
    assert book.cost("deepseek-v4-flash", 1_000_000, 1_000_000, at=weekend) == pytest.approx(peak / 2)


def test_announced_price_changes_apply_from_their_date():
    book = PriceBook()
    assert book.price_for("gemini-4-carbon", on=date(2026, 11, 30)).input == 2
    assert book.price_for("gemini-4-carbon", on=date(2026, 12, 1)).input == 4


def test_long_prompts_use_the_long_context_price():
    book = PriceBook()
    short = book.cost("claude-haiku-5-5", 100_000, 0)
    long = book.cost("claude-haiku-5-5", 100_001, 0)
    assert short == pytest.approx(0.1 * 0.10)
    assert long == pytest.approx(100_001 / 1_000_000 * 0.50)


def test_own_prices_add_models_and_replace_list_prices():
    book = PriceBook.from_entries(["kimi-k3=3/15/0.3", "GPT-5=1/2"])
    assert book.price_for("kimi-k3") == ModelPrice(3, 15, 0.3)
    # Without a cache price, cached tokens are billed as input.
    assert book.cost("gpt-5", 1_000_000, 0, 1_000_000) == pytest.approx(1)


@pytest.mark.parametrize("entry", ["kimi", "kimi=3", "kimi=a/b", "=1/2", "kimi=-1/2", "kimi=1/2/3/4"])
def test_malformed_price_entries_are_rejected(entry):
    with pytest.raises(PriceError):
        parse_price_entry(entry)


def test_runtime_skips_a_malformed_entry_and_keeps_the_rest(caplog):
    book = _price_book(["broken", "kimi-k3=3/15"])
    assert book.price_for("kimi-k3") == ModelPrice(3, 15)
    assert book.price_for("gpt-5") is not None
    assert "Ignored llm.prices entry" in caplog.text


@pytest.mark.parametrize(
    ("usage", "prompt", "cached"),
    [
        ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 60}}, 100, 60),
        ({"input_tokens": 100, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 70}}, 100, 70),
        ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80}, 100, 80),
        # Anthropic counts input without cache reads and writes.
        ({"input_tokens": 20, "output_tokens": 5, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 10}, 100, 70),
        (SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105, prompt_tokens_details=SimpleNamespace(cached_tokens=30)), 100, 30),
        ({"prompt_tokens": 100, "completion_tokens": 5}, 100, 0),
    ],
)
def test_cache_hits_are_read_from_each_provider_shape(usage, prompt, cached):
    record = usage_from_provider(usage)
    assert (record.prompt_tokens, record.cache_read_tokens, record.total_tokens) == (prompt, cached, prompt + 5)


def test_usage_without_a_cost_counts_as_unpriced():
    assert usage_from_provider({"prompt_tokens": 1}).unpriced_requests == 1
    priced = usage_from_provider({"prompt_tokens": 1}, cost_usd=0.5)
    assert (priced.cost_usd, priced.unpriced_requests, priced.cost_known) == (0.5, 0, True)
    free = usage_from_provider({"prompt_tokens": 1}, cost_usd=0.0)
    assert free.cost_known


def test_client_prices_usage_with_the_model_of_the_request():
    client = LLMClient(base_url="http://127.0.0.1:9/v1", api_key="test", model="gpt-5")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert client._priced(UsageEvent(usage), "gpt-5").cost_usd is None  # no price book yet
    client.price_book = PriceBook()
    assert client._priced(UsageEvent(usage), "gpt-5").cost_usd == pytest.approx(1.25)
    assert client._priced(UsageEvent(usage), "unknown-model").cost_usd is None


def test_session_totals_keep_cost_unknown_once_any_request_was_unpriced(tmp_path):
    session = Session.create("cost", tmp_path)
    session.add_usage(UsageRecord(10, 1, 11, cache_read_tokens=4, cost_usd=0.25))
    assert session.task_usage().cost_known
    session.add_usage(UsageRecord(10, 1, 11, unpriced_requests=1))
    total = session.task_usage()
    assert (total.cache_read_tokens, total.cost_usd, total.cost_known) == (4, 0.25, False)


def test_checkpoints_keep_cost_and_read_ones_written_before_it(tmp_path):
    session = Session.create("cost", tmp_path)
    session.add_usage(UsageRecord(10, 1, 11, cache_read_tokens=4, cost_usd=0.25, unpriced_requests=1))
    store = FileSessionRepository(tmp_path / "checkpoints")
    path = store.save(session)
    assert store.load(session.session_id).total_usage == session.total_usage

    data = json.loads(path.read_text(encoding="utf-8"))
    saved = data["session"]
    for name in ("cache_read_tokens", "cost_usd", "unpriced_requests"):
        saved["total_usage"].pop(name)
    saved.pop("disabled_mcp_servers")
    path.write_text(json.dumps(data), encoding="utf-8")
    restored = store.load(session.session_id)
    # Usage saved before pricing existed has an unknown cost, not a zero one.
    assert restored.total_usage == UsageRecord(10, 1, 11, unpriced_requests=1)
    assert not restored.task_usage().cost_known
    assert restored.disabled_mcp_servers == []
