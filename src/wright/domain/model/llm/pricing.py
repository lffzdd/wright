"""List prices of common models, and the cost of one request.

Prices are USD per million tokens, taken from each provider's official
pricing page on ``PRICES_CHECKED``:

- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI: https://developers.openai.com/api/docs/pricing (standard tier, short context)
- Google: https://aistudio.google.com/docs/pricing (paid tier, prompts up to 200K)
- DeepSeek: https://api-docs.deepseek.com/quick_start/pricing
- Zhipu: https://docs.z.ai/guides/overview/pricing
- Alibaba Qwen: https://www.alibabacloud.com/help/en/model-studio/model-pricing
  (international deployment, lowest input tier)

A gateway or reseller may charge differently, and the surcharges this table
leaves out (cache writes, long-context tiers other than Haiku 5.5's, batch and
priority tiers) are not counted, so the result is an estimate. ``llm.prices``
in settings adds models or replaces these prices.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .model_names import name_and_bare, normalize_model_name, snapshot_family

PRICES_CHECKED = "2026-11-18"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens. ``cache_read`` None bills cached tokens as input."""

    input: float
    output: float
    cache_read: float | None = None
    # Prompts longer than this many tokens are billed at ``long_context``.
    long_context_above: int | None = None
    long_context: ModelPrice | None = None
    # UTC (weekday set, start hour, end hour) windows at full price; any other
    # time is billed at ``off_peak_factor``. Empty means one price all day.
    peak_hours_utc: tuple[tuple[frozenset[int], int, int], ...] = ()
    off_peak_factor: float = 1.0

    def cost(self, prompt_tokens: int, completion_tokens: int, cache_read_tokens: int = 0, *, at: datetime | None = None) -> float:
        price = self
        if self.long_context is not None and self.long_context_above is not None and prompt_tokens > self.long_context_above:
            price = self.long_context
        cached = min(max(0, cache_read_tokens), max(0, prompt_tokens))
        cache_rate = price.input if price.cache_read is None else price.cache_read
        total = (
            (prompt_tokens - cached) * price.input
            + cached * cache_rate
            + max(0, completion_tokens) * price.output
        ) / 1_000_000
        return total * self._time_factor(at)

    def _time_factor(self, at: datetime | None) -> float:
        if not self.peak_hours_utc:
            return 1.0
        moment = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        for weekdays, start, end in self.peak_hours_utc:
            if moment.weekday() in weekdays and start <= moment.hour < end:
                return 1.0
        return self.off_peak_factor


@dataclass(frozen=True)
class PriceSchedule:
    """A price, and the prices a provider has announced for later dates."""

    current: ModelPrice
    changes: tuple[tuple[date, ModelPrice], ...] = ()

    def on(self, day: date) -> ModelPrice:
        price = self.current
        for since, later in sorted(self.changes, key=lambda item: item[0]):
            if day >= since:
                price = later
        return price


def _p(input: float, output: float, cache_read: float | None = None, **extra) -> ModelPrice:
    return ModelPrice(input, output, cache_read, **extra)


_WEEKDAYS = frozenset(range(5))
# DeepSeek: full price 01:00-04:00 and 06:00-10:00 UTC on weekdays, half price
# otherwise. Chinese public holidays are also off-peak; they are not modeled.
_DEEPSEEK_PEAK = ((_WEEKDAYS, 1, 4), (_WEEKDAYS, 6, 10))


def _deepseek(input: float, output: float, cache_read: float) -> ModelPrice:
    return _p(input, output, cache_read, peak_hours_utc=_DEEPSEEK_PEAK, off_peak_factor=0.5)


def _same(price: ModelPrice, *names: str) -> dict[str, ModelPrice]:
    return dict.fromkeys(names, price)


_CATALOG: dict[str, ModelPrice | PriceSchedule] = {
    # Anthropic
    **_same(_p(8, 40, 0.20), "claude-fable-5-2", "claude-mythos-5-2"),
    **_same(_p(10, 50, 0.25), "claude-fable-5-1", "claude-mythos-5-1"),
    **_same(_p(10, 50, 1), "claude-fable-5", "claude-mythos-5"),
    "claude-opus-5-5": _p(4, 20, 0.20),
    **_same(_p(5, 25, 0.50), "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"),
    "claude-sonnet-5-5": _p(2, 10, 0.10),
    "claude-sonnet-5": _p(2, 10, 0.20),
    **_same(_p(3, 15, 0.30), "claude-sonnet-4-6", "claude-sonnet-4-5"),
    "claude-haiku-5-5": _p(0.10, 0.50, 0.01, long_context_above=100_000, long_context=_p(0.50, 2.50, 0.05)),
    "claude-haiku-4-5": _p(1, 5, 0.10),
    # OpenAI
    "gpt-6.2-astra": _p(8, 40, 0.40),
    "gpt-6.2-sol": _p(2, 10, 0.10),
    "gpt-6-astra": _p(10, 50, 1),
    "gpt-6.1-sol": _p(2, 10, 0.10),
    "gpt-6.1-terra": _p(1, 5, 0.05),
    "gpt-6-luna": _p(0.10, 0.50, 0.01),
    "gpt-6-sol": _p(2, 10, 0.20),
    "gpt-5.6-sol": _p(4, 20, 0.40),
    "gpt-5.6-terra": _p(2, 12, 0.20),
    "gpt-5.6-luna": _p(0.20, 1.20, 0.02),
    "gpt-5.5": _p(5, 30, 0.50),
    "gpt-5.5-pro": _p(30, 180),
    "gpt-5.4": _p(2.50, 15, 0.25),
    "gpt-5.4-pro": _p(30, 180),
    "gpt-5.4-mini": _p(0.75, 4.50, 0.075),
    "gpt-5.4-nano": _p(0.20, 1.25, 0.02),
    **_same(_p(1.75, 14, 0.175), "gpt-5.2", "gpt-5.3-codex"),
    "gpt-5.2-pro": _p(21, 168),
    **_same(_p(1.25, 10, 0.125), "gpt-5.1", "gpt-5"),
    "gpt-5-mini": _p(0.25, 2, 0.025),
    "gpt-5-nano": _p(0.05, 0.40, 0.005),
    "gpt-5-pro": _p(15, 120),
    "gpt-4.1": _p(2, 8, 0.50),
    "gpt-4.1-mini": _p(0.40, 1.60, 0.10),
    "gpt-4o": _p(2.50, 10, 1.25),
    "gpt-4o-mini": _p(0.15, 0.60, 0.075),
    "o3": _p(2, 8, 0.50),
    "o3-pro": _p(20, 80),
    # Google
    **_same(PriceSchedule(_p(2, 10, 0.10), ((date(2026, 12, 1), _p(4, 20, 0.20)),)), "gemini-4-carbon", "gemini-4-argon"),
    "gemini-4-neon": _p(3, 15, 0.15),
    "gemini-4-hydrogen": _p(1.50, 7.50, 0.075),
    **_same(PriceSchedule(_p(0.75, 3.75, 0.075), ((date(2027, 1, 1), _p(1.50, 7.50, 0.15)),)), "gemini-3.8-flash", "gemini-3.6-flash"),
    "gemini-3.5-flash-lite": _p(0.30, 2.50, 0.03),
    "gemini-3.1-flash-lite": _p(0.25, 1.50, 0.025),
    "gemini-3.1-pro-preview": _p(2, 12, 0.20),
    "gemini-3-flash-preview": _p(0.50, 3, 0.05),
    "gemini-2.5-pro": _p(1.25, 10, 0.125),
    "gemini-2.5-flash": _p(0.30, 2.50, 0.03),
    "gemini-2.5-flash-lite": _p(0.10, 0.40, 0.01),
    # DeepSeek (the old v4-flash names are billed as deepseek-flash)
    **_same(_deepseek(0.30, 1.20, 0.006), "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"),
    "deepseek-v4-pro": _deepseek(1.32, 3.96, 0.044),
    # Zhipu GLM
    **_same(_p(1.40, 4.40, 0.26), "glm-5.3", "glm-5.2", "glm-5.1"),
    "glm-5.3-flash": _p(0.15, 0.50, 0.03),
    "glm-5.3-flashx": _p(0.37, 1.25, 0.075),
    "glm-5": _p(1, 3.20, 0.20),
    **_same(_p(0.60, 2.20, 0.11), "glm-4.7", "glm-4.6", "glm-4.5"),
    "glm-4.5-air": _p(0.20, 1.10, 0.03),
    **_same(_p(0, 0, 0), "glm-4.7-flash", "glm-4.5-flash"),
    # Alibaba Qwen; no cache price is published for most, so cached tokens count as input.
    "qwen4-max": _p(1.50, 4.50, 0.075),
    "qwen3.8-max": _p(2, 6),
    "qwen3.7-max": _p(2.50, 7.50),
    "qwen-max": _p(1.60, 6.40),
    "qwen3.7-plus": _p(0.40, 1.60),
    "qwen3.6-plus": _p(0.50, 3),
    "qwen3.5-plus": _p(0.40, 2.40),
    "qwen3.8-flash": _p(0.15, 0.47),
    "qwen3.6-flash": _p(0.25, 1.50),
    "qwen3.5-flash": _p(0.10, 0.40),
}

class PriceError(ValueError):
    pass


def parse_price_entry(entry: str) -> tuple[str, ModelPrice]:
    """``model=input/output[/cache_read]``, USD per million tokens."""
    name, sep, values = entry.partition("=")
    name = name.strip()
    parts = [part.strip() for part in values.split("/")]
    if not sep or not name or len(parts) not in {2, 3}:
        raise PriceError(f"price entry {entry!r} must look like model=input/output[/cache_read]")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise PriceError(f"price entry {entry!r} has a value that is not a number") from None
    if any(number < 0 for number in numbers):
        raise PriceError(f"price entry {entry!r} has a negative price")
    return name, ModelPrice(numbers[0], numbers[1], numbers[2] if len(numbers) == 3 else None)


class PriceBook:
    """Built-in list prices, with the person's own entries taking precedence."""

    def __init__(self, overrides: Mapping[str, ModelPrice] | None = None, catalog: Mapping[str, ModelPrice | PriceSchedule] | None = None) -> None:
        self._overrides = {name.lower(): price for name, price in (overrides or {}).items()}
        self._catalog = {name.lower(): price for name, price in (catalog if catalog is not None else _CATALOG).items()}

    @classmethod
    def from_entries(cls, entries: Iterable[str]) -> PriceBook:
        return cls(dict(parse_price_entry(entry) for entry in entries))

    def price_for(self, model: str, *, on: date | None = None) -> ModelPrice | None:
        key = normalize_model_name(model)
        if not key:
            return None
        if key in self._overrides:
            return self._overrides[key]
        # Gateways such as OpenRouter prefix the vendor: "openai/gpt-5".
        # A dated snapshot costs what its family does.
        for candidate in name_and_bare(key):
            found = self._catalog.get(candidate)
            if found is None:
                family = snapshot_family(candidate)
                found = self._overrides.get(family) or self._catalog.get(family)
            if found is not None:
                return found.on(on or datetime.now(timezone.utc).date()) if isinstance(found, PriceSchedule) else found
        return None

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int, cache_read_tokens: int = 0, *, at: datetime | None = None) -> float | None:
        moment = at or datetime.now(timezone.utc)
        price = self.price_for(model, on=moment.date())
        if price is None:
            return None
        return price.cost(prompt_tokens, completion_tokens, cache_read_tokens, at=moment)


__all__ = ["PRICES_CHECKED", "ModelPrice", "PriceBook", "PriceError", "PriceSchedule", "parse_price_entry"]
