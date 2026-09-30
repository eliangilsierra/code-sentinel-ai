"""Cost of a run expressed as API-equivalent dollars, computed from token usage."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PRICING = Path(__file__).resolve().parent.parent / "pricing.json"
_SUFFIX = re.compile(r"\[[^\]]*\]$")


class PricingError(Exception):
    """Raised when a model has no price or a usage record is malformed."""


@dataclass(frozen=True)
class Usage:
    """Token counts of one model in one run."""

    model: str
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    reported_cost: float | None = None


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


class Pricing:
    """A table of model prices."""

    def __init__(self, unit_tokens: int, models: dict[str, Price]) -> None:
        self._unit = unit_tokens
        self._models = models

    @classmethod
    def load(cls, path: Path = DEFAULT_PRICING) -> Pricing:
        data = json.loads(path.read_text(encoding="utf-8"))
        models = {name: Price(**prices) for name, prices in data["models"].items()}
        return cls(int(data["unit_tokens"]), models)

    def price_of(self, model: str) -> Price:
        """Return the price of ``model``, matching by exact id or by the longest id prefix."""
        name = _SUFFIX.sub("", model)
        if name in self._models:
            return self._models[name]
        prefixes = [known for known in self._models if name.startswith(known)]
        if not prefixes:
            raise PricingError(f"no price for model {model!r}")
        return self._models[max(prefixes, key=len)]

    def cost(self, usage: Usage) -> float:
        price = self.price_of(usage.model)
        total = (
            usage.input * price.input
            + usage.cache_write_5m * price.cache_write_5m
            + usage.cache_write_1h * price.cache_write_1h
            + usage.cache_read * price.cache_read
            + usage.output * price.output
        )
        return total / self._unit


def parse_usage(result: dict[str, Any], fallback_model: str | None = None) -> list[Usage]:
    """Extract per-model usage from the JSON result of ``claude -p --output-format json``.

    ``modelUsage`` covers the whole agent tree, including subagents, and is preferred. Its cache
    creation tokens carry no time-to-live, so they are counted at the five-minute rate. The
    top-level ``usage`` excludes subagents; it is only used, with ``fallback_model``, when
    ``modelUsage`` is empty.
    """
    model_usage = result.get("modelUsage") or {}
    if model_usage:
        return [_from_model_usage(model, entry) for model, entry in sorted(model_usage.items())]
    usage = result.get("usage") or {}
    if not _has_tokens(usage):
        return []
    if fallback_model is None:
        raise PricingError("result has usage but no modelUsage and no fallback model was given")
    creation = usage.get("cache_creation") or {}
    write_5m = creation.get("ephemeral_5m_input_tokens", 0)
    write_1h = creation.get("ephemeral_1h_input_tokens", 0)
    if not creation:
        write_5m = usage.get("cache_creation_input_tokens", 0)
    return [
        Usage(
            model=fallback_model,
            input=usage.get("input_tokens", 0),
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            cache_read=usage.get("cache_read_input_tokens", 0),
            output=usage.get("output_tokens", 0),
            reported_cost=result.get("total_cost_usd"),
        )
    ]


def _from_model_usage(model: str, entry: dict[str, Any]) -> Usage:
    try:
        return Usage(
            model=model,
            input=entry["inputTokens"],
            cache_write_5m=entry.get("cacheCreationInputTokens", 0),
            cache_read=entry.get("cacheReadInputTokens", 0),
            output=entry["outputTokens"],
            reported_cost=entry.get("costUSD"),
        )
    except KeyError as error:
        raise PricingError(f"modelUsage entry for {model!r} lacks {error}") from error


def _has_tokens(usage: dict[str, Any]) -> bool:
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return any(usage.get(key, 0) for key in keys)


def equivalent_cost(usages: Iterable[Usage], pricing: Pricing) -> float:
    """Total API-equivalent dollars of ``usages``."""
    return sum(pricing.cost(usage) for usage in usages)


def reported_cost(usages: Iterable[Usage]) -> float | None:
    """Sum of the costs reported by the client, or ``None`` if none was reported."""
    values = [usage.reported_cost for usage in usages if usage.reported_cost is not None]
    return sum(values) if values else None


def cache_read_ratio(usages: Iterable[Usage]) -> float | None:
    """Share of input-side tokens served from cache: reads over reads, writes and fresh input."""
    usages = list(usages)
    reads = sum(usage.cache_read for usage in usages)
    total = reads + sum(u.input + u.cache_write_5m + u.cache_write_1h for u in usages)
    return reads / total if total else None


def total_tokens(usages: Iterable[Usage]) -> int:
    """All tokens of a run, input side and output."""
    return sum(
        u.input + u.cache_write_5m + u.cache_write_1h + u.cache_read + u.output for u in usages
    )
