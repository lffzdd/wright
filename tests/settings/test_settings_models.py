"""The provider and catalog overview the settings editor shows."""

from __future__ import annotations

from wright.application.settings.models import model_overview
from wright.domain.model.settings import LlmSettings


def _by_id(view):
    return {item["id"]: item for item in view["providers"]}


def test_providers_report_whether_their_key_is_set_without_the_key():
    view = model_overview(LlmSettings(), {"ANTHROPIC_API_KEY": "sk-secret", "DEEPSEEK_API_KEY": "  "})
    providers = _by_id(view)
    assert providers["anthropic"]["key_set"] is True
    assert providers["anthropic"]["reachable"] is True
    # A blank key counts as unset.
    assert providers["deepseek"]["key_set"] is False
    assert providers["openai"]["reachable"] is False
    # Qwen's endpoint depends on the account, so a key alone does not reach it.
    assert _by_id(model_overview(LlmSettings(), {"DASHSCOPE_API_KEY": "k"}))["qwen"]["reachable"] is False
    assert "sk-secret" not in repr(view)


def test_custom_endpoint_reaches_only_the_provider_that_serves_it():
    llm = LlmSettings(base_url="https://api.deepseek.com/v1")
    view = model_overview(llm, {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"})
    assert view["endpoint"] == {
        "base_url": "https://api.deepseek.com/v1", "key_env": "OPENAI_API_KEY",
        "key_set": True, "provider": "deepseek",
    }
    reachable = [item["id"] for item in view["providers"] if item["reachable"]]
    assert reachable == ["deepseek"]


def test_models_carry_context_window_and_price_with_user_overrides():
    llm = LlmSettings(prices=("gpt-4o=9/18", "not a price"))
    models = {item["name"]: item for item in model_overview(llm, {})["models"]}
    assert models["gpt-4o"]["provider"] == "openai"
    assert models["gpt-4o"]["context_window"] == 128_000
    assert models["gpt-4o"]["price"] == {"input": 9.0, "output": 18.0, "cache_read": None}
    assert models["claude-opus-5-5"]["preset"] is True


def test_models_that_would_stop_a_session_are_reported_with_the_routing_error():
    llm = LlmSettings(model="claude-opus-5-5", memory_model="gpt-4o")
    problems = model_overview(llm, {"ANTHROPIC_API_KEY": "k"})["problems"]
    assert "llm.model" not in problems
    assert "OPENAI_API_KEY" in problems["llm.memory_model"]
    assert "not in Wright's model catalog" in model_overview(LlmSettings(model="mystery"), {})["problems"]["llm.model"]
