"""What a settings editor shows about providers and catalog models.

Only whether each provider's key is set, never the key itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...domain.model.llm.catalog import BASE_URL_KEY_ENV, ModelCatalog
from ...domain.model.llm.pricing import (
    ModelPrice,
    PriceBook,
    PriceError,
    parse_price_entry,
)
from ...domain.model.settings import LlmSettings


def _price_book(entries: tuple[str, ...]) -> PriceBook:
    overrides: dict[str, ModelPrice] = {}
    for entry in entries:
        try:
            name, price = parse_price_entry(entry)
        except PriceError:
            # The settings warnings already name a malformed entry.
            continue
        overrides[name] = price
    return PriceBook(overrides)


def _price(price: ModelPrice | None) -> dict[str, float | None] | None:
    if price is None:
        return None
    return {"input": price.input, "output": price.output, "cache_read": price.cache_read}


def model_overview(
    llm: LlmSettings,
    environ: Mapping[str, str],
    catalog: ModelCatalog | None = None,
) -> dict[str, Any]:
    """Providers with their key status, the custom endpoint, and every catalog model."""
    catalog = catalog or ModelCatalog()
    prices = _price_book(llm.prices)

    def key_set(name: str) -> bool:
        return bool(environ.get(name, "").strip())

    endpoint_provider = catalog.provider_for_url(llm.base_url) if llm.base_url else None
    providers = []
    for item in catalog.providers:
        if llm.base_url:
            reachable = endpoint_provider is not None and endpoint_provider.id == item.id
        else:
            reachable = item.base_url is not None and key_set(item.key_env)
        providers.append({
            "id": item.id,
            "name": item.name,
            "base_url": item.base_url,
            "key_env": item.key_env,
            "key_set": key_set(item.key_env),
            "reachable": reachable,
        })
    models = [
        {
            "name": item.name,
            "provider": item.provider,
            "context_window": item.context_window,
            "preset": item.preset,
            "price": _price(prices.price_for(item.name)),
        }
        for item in catalog.models()
    ]
    return {
        "endpoint": {
            "base_url": llm.base_url,
            "key_env": BASE_URL_KEY_ENV,
            "key_set": key_set(BASE_URL_KEY_ENV),
            "provider": endpoint_provider.id if endpoint_provider is not None else "",
        },
        "providers": providers,
        "models": models,
    }


__all__ = ["model_overview"]
