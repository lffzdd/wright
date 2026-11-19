"""Built-in model catalog: endpoint, key, context window and picker presets."""

from __future__ import annotations

import threading
from copy import copy
from types import SimpleNamespace

import pytest

from wright.application.session.service import SessionServiceError, set_session_model
from wright.domain.model.llm.catalog import (
    DEFAULT_CONTEXT_LIMIT,
    PROVIDERS,
    ModelCatalog,
    ModelRouteError,
    ModelRouter,
    parse_context_entry,
)
from wright.domain.model.llm.model_adapters import available_models
from wright.domain.model.llm.pricing import _CATALOG as PRICED
from wright.domain.model.session import Session
from wright.domain.model.settings import LlmSettings
from wright.infrastructure.llm.llm import LLMClient

KEYS = {"OPENAI_API_KEY": "sk-openai", "DEEPSEEK_API_KEY": "sk-deepseek", "ANTHROPIC_API_KEY": "sk-ant"}


def _router(keys=KEYS, overrides=None, **llm) -> ModelRouter:
    return ModelRouter(LlmSettings(**llm), keys, context_overrides=overrides)


def test_every_priced_model_is_in_the_catalog_with_a_known_provider():
    catalog = ModelCatalog()
    provider_ids = {item.id for item in PROVIDERS}
    for name in PRICED:
        found = catalog.model(name)
        assert found is not None, name
        assert found.provider in provider_ids


def test_catalog_model_goes_to_its_provider_with_its_own_key():
    route = _router().route("deepseek-v4-pro")
    assert route.base_url == "https://api.deepseek.com"
    assert route.api_key == "sk-deepseek"
    assert route.provider == "deepseek"
    assert route.context_limit == 1_000_000


def test_gateway_prefix_and_dated_snapshot_find_the_family():
    router = _router()
    assert router.route("anthropic/claude-haiku-4-5-20251001").base_url == "https://api.anthropic.com/v1/"
    assert router.context_limit("claude-haiku-4-5-20251001") == 200_000


def test_missing_provider_key_fails_without_falling_back_to_openai_key():
    with pytest.raises(ModelRouteError, match="ZAI_API_KEY"):
        _router().route("glm-5.3")


def test_base_url_wins_for_every_model_and_keeps_openai_key():
    router = _router(base_url="https://gateway.example/v1")
    route = router.route("deepseek-v4-pro")
    assert (route.base_url, route.api_key, route.provider) == ("https://gateway.example/v1", "sk-openai", "")
    # The catalog still supplies the context window.
    assert route.context_limit == 1_000_000
    with pytest.raises(ModelRouteError, match="OPENAI_API_KEY"):
        _router(keys={"DEEPSEEK_API_KEY": "x"}, base_url="https://api.deepseek.com").route("deepseek-flash")


def test_unknown_model_without_base_url_asks_for_one():
    with pytest.raises(ModelRouteError, match=r"llm\.base_url"):
        _router().route("my-local-model")


def test_qwen_endpoint_depends_on_the_account_so_it_needs_base_url():
    with pytest.raises(ModelRouteError, match=r"llm\.base_url"):
        _router(keys={"DASHSCOPE_API_KEY": "x"}).route("qwen4-max")
    route = _router(base_url="https://ws1.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1").route("qwen4-max")
    assert route.provider == "qwen"


def test_context_limit_precedence():
    assert _router().context_limit("my-local-model") == DEFAULT_CONTEXT_LIMIT
    assert _router().context_limit("gpt-4o") == 128_000
    assert _router().context_limit("claude-opus-5-5") == 1_000_000
    # A published window of None falls back to the default.
    assert _router().context_limit("gpt-5-mini") == DEFAULT_CONTEXT_LIMIT
    assert _router(context_limit=64_000).context_limit("claude-opus-5-5") == 64_000
    per_model = _router(context_limit=64_000, overrides={"Claude-Opus-5-5": 500_000})
    assert per_model.context_limit("claude-opus-5-5") == 500_000
    assert per_model.context_limit("anthropic/claude-opus-5-5") == 500_000
    assert per_model.context_limit("gpt-4o") == 64_000


@pytest.mark.parametrize(("entry", "expected"), [
    ("my-model=64000", ("my-model", 64000)),
    (" my-model = 1_000_000 ", ("my-model", 1_000_000)),
])
def test_parse_context_entry(entry, expected):
    assert parse_context_entry(entry) == expected


@pytest.mark.parametrize("entry", ["my-model", "=5", "m=0", "m=-1", "m=lots"])
def test_parse_context_entry_rejects_malformed(entry):
    with pytest.raises(ValueError):
        parse_context_entry(entry)


def test_presets_follow_reachable_providers():
    assert _router(keys={"DEEPSEEK_API_KEY": "x"}).presets() == ("deepseek-v4-pro", "deepseek-flash")
    # Qwen has a key but no endpoint, so it offers nothing without base_url.
    assert _router(keys={"DASHSCOPE_API_KEY": "x"}).presets() == ()
    assert _router(keys={}).presets() == ()
    by_host = _router(keys={}, base_url="https://api.z.ai/api/paas/v4/").presets()
    assert by_host == ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx")
    assert _router(base_url="https://gateway.example/v1").presets() == ()


def test_available_models_prefers_configured_list_over_presets():
    presets = ("a", "b")
    assert available_models("b", (), presets) == ("a", "b")
    assert available_models("x", (), presets) == ("x", "a", "b")
    assert available_models("x", ("m",), presets) == ("x", "m")


def _client(model="gpt-6.2-sol", router=None) -> LLMClient:
    router = router or _router()
    route = router.route(model)
    client = LLMClient(
        base_url=route.base_url, api_key=route.api_key, model=model,
        context_limit=route.context_limit,
    )
    client.router = router.route
    return client


def test_retarget_moves_to_the_new_provider_and_undo_restores():
    client = _client()
    assert client.transport_name == "responses"
    sdk = client.client
    undo = client.retarget("claude-sonnet-5-5")
    assert client.base_url == "https://api.anthropic.com/v1/"
    assert client.transport_name == "chat"
    assert client.model == "claude-sonnet-5-5"
    assert client.client is not sdk
    undo()
    assert (client.base_url, client.model, client.transport_name) == ("https://api.openai.com/v1", "gpt-6.2-sol", "responses")
    assert client.client is sdk


def test_retarget_same_provider_keeps_the_sdk_client_and_updates_context():
    client = _client("gpt-4o")
    sdk = client.client
    client.retarget("gpt-6.2-astra")
    assert client.client is sdk
    assert client.context_limit == 1_050_000


def test_unreachable_model_leaves_the_client_unchanged():
    client = _client()
    with pytest.raises(ModelRouteError):
        client.retarget("glm-5.3")
    assert client.model == "gpt-6.2-sol"


def test_retargeting_a_copy_leaves_the_shared_client_alone():
    shared = _client()
    run = copy(shared)
    run.retarget("deepseek-flash")
    assert shared.base_url == "https://api.openai.com/v1"
    assert shared.model == "gpt-6.2-sol"
    assert run.base_url == "https://api.deepseek.com"


def _runtime(client, tmp_path):
    idle = threading.Event()
    idle.set()
    session = Session.create("goal", tmp_path)
    session.llm_transport = client.transport_name
    return SimpleNamespace(
        agent_idle=idle, llm=client, session_state=session,
        agent=SimpleNamespace(llm=client, checkpoint_store=None), publisher=None,
    )


def test_switching_model_moves_the_session_to_the_new_provider(tmp_path):
    runtime = _runtime(_client(), tmp_path)
    set_session_model(runtime, "deepseek-v4-pro")
    assert runtime.llm.base_url == "https://api.deepseek.com"
    assert runtime.session_state.model_name == "deepseek-v4-pro"
    assert runtime.session_state.llm_transport == "chat"


def test_switch_refused_when_history_needs_the_responses_api(tmp_path):
    runtime = _runtime(_client(), tmp_path)
    runtime.session_state.model_name = "gpt-6.2-sol"
    runtime.session_state.append_message({
        "role": "assistant", "content": "hi",
        "provider_state": {"responses_output": [{"type": "message"}]},
    })
    with pytest.raises(SessionServiceError, match="Responses API"):
        set_session_model(runtime, "claude-sonnet-5-5")
    assert runtime.llm.base_url == "https://api.openai.com/v1"
    assert runtime.llm.model == "gpt-6.2-sol"
    assert runtime.session_state.model_name == "gpt-6.2-sol"
    # Another Responses model is fine.
    set_session_model(runtime, "gpt-6-luna")
    assert runtime.llm.model == "gpt-6-luna"


def test_switch_to_unreachable_model_reports_the_missing_key(tmp_path):
    runtime = _runtime(_client(), tmp_path)
    with pytest.raises(SessionServiceError, match="ZAI_API_KEY"):
        set_session_model(runtime, "glm-5.3")
    assert runtime.llm.model == "gpt-6.2-sol"
