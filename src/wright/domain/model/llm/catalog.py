"""Built-in catalog of common models: provider, endpoint and context window.

With the catalog, naming a common model and setting its provider's API key is
enough: the endpoint, context window and model picker follow from it. Values
come from each provider's official documentation on ``CATALOG_CHECKED``:

- Anthropic: https://platform.claude.com/docs/en/about-claude/models/overview
  and https://platform.claude.com/docs/en/api/openai-sdk (endpoint)
- OpenAI: https://developers.openai.com/api/docs/models; the endpoint is the
  OpenAI SDK's default
- Google: https://aistudio.google.com/docs/models and
  https://aistudio.google.com/docs/openai (endpoint)
- DeepSeek: https://api-docs.deepseek.com/ and
  https://api-docs.deepseek.com/quick_start/pricing
- Zhipu: https://docs.z.ai/guides/overview/overview and
  https://docs.z.ai/guides/overview/quick-start (international endpoint)
- Alibaba Qwen: https://www.alibabacloud.com/help/en/model-studio/text-generation-model.
  Its OpenAI-compatible endpoint contains the account's workspace id, so Qwen
  models need ``llm.base_url``.

A model with no published context window has ``None`` here and falls back to
``DEFAULT_CONTEXT_LIMIT``. Where a provider publishes a smaller input limit
than the context window (GPT-5, Qwen4 Max), the input limit is used. Settings
override everything here: ``llm.base_url`` pins one endpoint for every model,
``llm.context_limit`` and ``llm.context_limits`` set context windows, and
``llm.models`` replaces the picker's presets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from ..settings import LlmSettings
from .model_names import name_and_bare, normalize_model_name, snapshot_family

CATALOG_CHECKED = "2026-11-18"
DEFAULT_CONTEXT_LIMIT = 128_000
# Key sent to ``llm.base_url``, exactly as before the catalog existed.
BASE_URL_KEY_ENV = "OPENAI_API_KEY"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    # OpenAI-compatible endpoint; None when it depends on the account.
    base_url: str | None
    key_env: str
    # Hosts (and their subdomains) that identify this provider in llm.base_url.
    hosts: tuple[str, ...]

    def serves(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == item or host.endswith("." + item) for item in self.hosts)


@dataclass(frozen=True)
class CatalogModel:
    name: str
    provider: str
    context_window: int | None = None
    # Offered by the model picker when llm.models is empty.
    preset: bool = False


PROVIDERS: tuple[Provider, ...] = (
    Provider("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY", ("api.openai.com",)),
    Provider("anthropic", "Anthropic", "https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY", ("api.anthropic.com",)),
    Provider(
        "google", "Google", "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY", ("generativelanguage.googleapis.com",),
    ),
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", ("api.deepseek.com",)),
    # Zhipu publishes no key variable name; ZAI_API_KEY is Wright's.
    Provider("zhipu", "Zhipu", "https://api.z.ai/api/paas/v4/", "ZAI_API_KEY", ("api.z.ai", "open.bigmodel.cn")),
    Provider("qwen", "Alibaba Qwen", None, "DASHSCOPE_API_KEY", ("aliyuncs.com",)),
)

_M = 1_000_000


def _models(provider: str, windows: Mapping[str, int | None], presets: Iterable[str] = ()) -> dict[str, CatalogModel]:
    chosen = set(presets)
    unknown = chosen - set(windows)
    if unknown:
        raise ValueError(f"presets not in the {provider} table: {sorted(unknown)}")
    return {
        name: CatalogModel(name, provider, window, name in chosen)
        for name, window in windows.items()
    }


_CATALOG: dict[str, CatalogModel] = {
    **_models("anthropic", {
        "claude-opus-5-5": _M, "claude-fable-5-2": _M, "claude-sonnet-5-5": _M,
        "claude-mythos-5-2": _M, "claude-fable-5-1": _M, "claude-mythos-5-1": None,
        "claude-fable-5": _M, "claude-mythos-5": None,
        "claude-opus-5": _M, "claude-opus-4-8": _M, "claude-opus-4-7": _M, "claude-opus-4-6": _M,
        "claude-opus-4-5": 200_000, "claude-sonnet-5": _M, "claude-sonnet-4-6": _M,
        "claude-sonnet-4-5": 200_000, "claude-haiku-5-5": _M, "claude-haiku-4-5": 200_000,
    }, presets=("claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5")),
    **_models("openai", {
        "gpt-6.2-astra": 1_050_000, "gpt-6.2-sol": 1_050_000, "gpt-6-luna": 1_050_000,
        "gpt-6-astra": None, "gpt-6.1-sol": None, "gpt-6.1-terra": 1_050_000, "gpt-6-sol": None,
        "gpt-5.6-sol": None, "gpt-5.6-terra": None, "gpt-5.6-luna": None,
        "gpt-5.5": 1_050_000, "gpt-5.5-pro": None, "gpt-5.4": 1_050_000, "gpt-5.4-pro": None,
        "gpt-5.4-mini": None, "gpt-5.4-nano": None, "gpt-5.3-codex": None,
        "gpt-5.2": None, "gpt-5.2-pro": None, "gpt-5.1": None,
        # 400K context, of which at most 272K is input.
        "gpt-5": 272_000, "gpt-5-mini": None, "gpt-5-nano": None, "gpt-5-pro": None,
        "gpt-4.1": 1_047_576, "gpt-4.1-mini": None, "gpt-4o": 128_000, "gpt-4o-mini": None,
        "o3": 200_000, "o3-pro": None,
    }, presets=("gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-luna")),
    **_models("google", {
        "gemini-4-carbon": _M, "gemini-4-argon": _M, "gemini-4-neon": _M,
        "gemini-4-hydrogen": 1_048_576, "gemini-3.8-flash": 1_048_576,
        "gemini-3.6-flash": None, "gemini-3.5-flash-lite": None, "gemini-3.1-flash-lite": None,
        "gemini-3.1-pro-preview": None, "gemini-3-flash-preview": None,
        "gemini-2.5-pro": 1_048_576, "gemini-2.5-flash": None, "gemini-2.5-flash-lite": None,
    }, presets=("gemini-4-carbon", "gemini-3.8-flash", "gemini-4-hydrogen")),
    **_models("deepseek", {
        "deepseek-v4-pro": _M, "deepseek-flash": _M,
        # Older names DeepSeek still serves as deepseek-flash.
        "deepseek-v4-flash": _M, "deepseek-v4-flash-vision-exp": _M,
    }, presets=("deepseek-v4-pro", "deepseek-flash")),
    **_models("zhipu", {
        "glm-5.3": _M, "glm-5.3-flash": _M, "glm-5.3-flashx": _M,
        "glm-5.2": _M, "glm-5.1": 200_000, "glm-5": 200_000,
        "glm-4.7": 200_000, "glm-4.6": 200_000, "glm-4.5": None, "glm-4.5-air": None,
        "glm-4.7-flash": None, "glm-4.5-flash": None,
    }, presets=("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx")),
    **_models("qwen", {
        # 1M context, of which at most 991,808 is input.
        "qwen4-max": 991_808, "qwen3.7-plus": _M, "qwen3.8-flash": _M,
        "qwen3.8-max": _M, "qwen3.7-max": _M, "qwen-max": 128_000,
        "qwen3.6-plus": _M, "qwen3.5-plus": _M, "qwen3.6-flash": _M, "qwen3.5-flash": _M,
    }, presets=("qwen4-max", "qwen3.7-plus", "qwen3.8-flash")),
}


class ModelRouteError(ValueError):
    """A model that cannot be reached with the current settings and keys."""


@dataclass(frozen=True)
class ModelRoute:
    """Where requests for one model go."""

    model: str
    base_url: str
    api_key: str
    context_limit: int
    # Catalog provider id; empty when the endpoint is not a known provider.
    provider: str = ""


def parse_context_entry(entry: str) -> tuple[str, int]:
    """``model=tokens``."""
    name, sep, value = entry.partition("=")
    name, value = name.strip(), value.strip().replace("_", "")
    if not sep or not name or not value.isdigit() or int(value) <= 0:
        raise ValueError(f"context limit entry {entry!r} must look like model=tokens with tokens above 0")
    return name, int(value)


class ModelCatalog:
    def __init__(
        self,
        models: Mapping[str, CatalogModel] | None = None,
        providers: Iterable[Provider] | None = None,
    ) -> None:
        self._models = {name.lower(): item for name, item in (models if models is not None else _CATALOG).items()}
        self._providers = {item.id: item for item in (providers if providers is not None else PROVIDERS)}

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers.values())

    def provider(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def provider_for_url(self, url: str) -> Provider | None:
        return next((item for item in self._providers.values() if item.serves(url)), None)

    def model(self, name: str) -> CatalogModel | None:
        for candidate in name_and_bare(normalize_model_name(name)):
            found = self._models.get(candidate) or self._models.get(snapshot_family(candidate))
            if found is not None:
                return found
        return None

    def presets(self, provider_ids: Iterable[str]) -> tuple[str, ...]:
        wanted = set(provider_ids)
        return tuple(
            item.name for item in self._models.values()
            if item.preset and item.provider in wanted
        )


class ModelRouter:
    """Settings first, then the catalog: endpoint, key and context window of a model."""

    def __init__(
        self,
        llm: LlmSettings,
        api_keys: Mapping[str, str],
        *,
        context_overrides: Mapping[str, int] | None = None,
        catalog: ModelCatalog | None = None,
    ) -> None:
        self._llm = llm
        self._keys = {name: value for name, value in api_keys.items() if value}
        self._context = {normalize_model_name(name): tokens for name, tokens in (context_overrides or {}).items()}
        self.catalog = catalog or ModelCatalog()

    def context_limit(self, model: str) -> int:
        """llm.context_limits, then llm.context_limit, then the catalog, then 128000."""
        for candidate in name_and_bare(normalize_model_name(model)):
            found = self._context.get(candidate) or self._context.get(snapshot_family(candidate))
            if found:
                return found
        if self._llm.context_limit > 0:
            return self._llm.context_limit
        known = self.catalog.model(model)
        if known is not None and known.context_window:
            return known.context_window
        return DEFAULT_CONTEXT_LIMIT

    def route(self, model: str) -> ModelRoute:
        name = model.strip()
        if not name:
            raise ModelRouteError("model name cannot be empty")
        limit = self.context_limit(name)
        if self._llm.base_url:
            # An explicit endpoint wins for every model and keeps its old key.
            key = self._require_key(BASE_URL_KEY_ENV, f"llm.base_url ({self._llm.base_url})")
            found = self.catalog.provider_for_url(self._llm.base_url)
            return ModelRoute(name, self._llm.base_url, key, limit, found.id if found else "")
        known = self.catalog.model(name)
        provider = self.catalog.provider(known.provider) if known is not None else None
        if provider is None:
            raise ModelRouteError(
                f"{name} is not in Wright's model catalog; set llm.base_url (OPENAI_BASE_URL) "
                f"to its OpenAI-compatible endpoint and {BASE_URL_KEY_ENV} to its key"
            )
        if provider.base_url is None:
            raise ModelRouteError(
                f"{provider.name}'s endpoint depends on your account; set llm.base_url (OPENAI_BASE_URL) "
                f"to it and {BASE_URL_KEY_ENV} to its key to use {name}"
            )
        # Never fall back to another provider's key.
        key = self._require_key(provider.key_env, f"{provider.name} ({name})")
        return ModelRoute(name, provider.base_url, key, limit, provider.id)

    def presets(self) -> tuple[str, ...]:
        """Catalog models the picker can offer with the current endpoint and keys."""
        if self._llm.base_url:
            found = self.catalog.provider_for_url(self._llm.base_url)
            return self.catalog.presets([found.id]) if found is not None else ()
        reachable = [
            item.id for item in self.catalog.providers
            if item.base_url is not None and item.key_env in self._keys
        ]
        return self.catalog.presets(reachable)

    def _require_key(self, env: str, purpose: str) -> str:
        key = self._keys.get(env)
        if not key:
            raise ModelRouteError(f"Set {env} in .env to use {purpose}")
        return key


def provider_key_envs(catalog: ModelCatalog | None = None) -> tuple[str, ...]:
    """Every environment variable that may hold a model API key."""
    catalog = catalog or ModelCatalog()
    return tuple(dict.fromkeys([BASE_URL_KEY_ENV, *(item.key_env for item in catalog.providers)]))


__all__ = [
    "BASE_URL_KEY_ENV",
    "CATALOG_CHECKED",
    "DEFAULT_CONTEXT_LIMIT",
    "PROVIDERS",
    "CatalogModel",
    "ModelCatalog",
    "ModelRoute",
    "ModelRouteError",
    "ModelRouter",
    "Provider",
    "parse_context_entry",
    "provider_key_envs",
]
