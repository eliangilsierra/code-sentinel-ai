from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.runner.cost import (
    DEFAULT_PRICING,
    Pricing,
    PricingError,
    Usage,
    cache_read_ratio,
    equivalent_cost,
    parse_usage,
    reported_cost,
    total_tokens,
)


@pytest.fixture(scope="module")
def pricing() -> Pricing:
    return Pricing.load(DEFAULT_PRICING)


def test_each_component_is_priced_per_million_tokens(pricing: Pricing) -> None:
    model = "claude-sonnet-5-5"
    assert pricing.cost(Usage(model, input=1_000_000)) == pytest.approx(2.0)
    assert pricing.cost(Usage(model, cache_write_5m=1_000_000)) == pytest.approx(2.5)
    assert pricing.cost(Usage(model, cache_write_1h=1_000_000)) == pytest.approx(4.0)
    assert pricing.cost(Usage(model, cache_read=1_000_000)) == pytest.approx(0.2)
    assert pricing.cost(Usage(model, output=1_000_000)) == pytest.approx(10.0)


def test_mixed_usage_is_summed(pricing: Pricing) -> None:
    usage = Usage(
        "claude-sonnet-5-5", input=10_000, cache_write_5m=20_000, cache_read=100_000, output=5_000
    )
    expected = (10_000 * 2.0 + 20_000 * 2.5 + 100_000 * 0.2 + 5_000 * 10.0) / 1_000_000
    assert pricing.cost(usage) == pytest.approx(expected)


def test_opus_and_haiku_have_their_own_prices(pricing: Pricing) -> None:
    assert pricing.cost(Usage("claude-opus-5-5", output=1_000_000)) == pytest.approx(20.0)
    assert pricing.cost(Usage("claude-haiku-4-5", cache_read=1_000_000)) == pytest.approx(0.1)


def test_model_ids_match_by_prefix_and_ignore_context_suffix(pricing: Pricing) -> None:
    assert pricing.price_of("claude-haiku-4-5-20251001").output == 5.0
    assert pricing.price_of("claude-sonnet-5-5[1m]").output == 10.0


def test_unknown_model_is_rejected(pricing: Pricing) -> None:
    with pytest.raises(PricingError, match="no price"):
        pricing.price_of("claude-unknown-9")


def test_pricing_table_lists_the_models_used_by_the_project(pricing: Pricing) -> None:
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5", "claude-fable-5-1"):
        pricing.price_of(model)


def test_pricing_file_has_positive_prices_and_write_above_read() -> None:
    data = json.loads(DEFAULT_PRICING.read_text(encoding="utf-8"))
    for name, price in data["models"].items():
        assert all(value > 0 for value in price.values()), name
        assert (
            price["cache_write_1h"] > price["cache_write_5m"] > price["input"] > price["cache_read"]
        )


MODEL_USAGE_RESULT = {
    "total_cost_usd": 0.05,
    "usage": {"input_tokens": 1, "output_tokens": 1},
    "modelUsage": {
        "claude-sonnet-5-5": {
            "inputTokens": 10_000,
            "outputTokens": 2_000,
            "cacheReadInputTokens": 50_000,
            "cacheCreationInputTokens": 8_000,
            "costUSD": 0.03,
        },
        "claude-haiku-4-5-20251001": {
            "inputTokens": 1_000,
            "outputTokens": 500,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.004,
        },
    },
}


def test_model_usage_is_preferred_over_top_level_usage(pricing: Pricing) -> None:
    usages = parse_usage(MODEL_USAGE_RESULT)
    assert [u.model for u in usages] == ["claude-haiku-4-5-20251001", "claude-sonnet-5-5"]
    sonnet = usages[1]
    assert (sonnet.input, sonnet.output, sonnet.cache_read, sonnet.cache_write_5m) == (
        10_000,
        2_000,
        50_000,
        8_000,
    )
    expected = (10_000 * 2 + 8_000 * 2.5 + 50_000 * 0.2 + 2_000 * 10) / 1e6 + (
        1_000 * 1 + 500 * 5
    ) / 1e6
    assert equivalent_cost(usages, pricing) == pytest.approx(expected)


def test_reported_cost_sums_client_estimates() -> None:
    assert reported_cost(parse_usage(MODEL_USAGE_RESULT)) == pytest.approx(0.034)
    assert reported_cost([Usage("claude-sonnet-5-5")]) is None


def test_top_level_usage_is_used_only_with_a_fallback_model(pricing: Pricing) -> None:
    result = {
        "total_cost_usd": 0.01,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100},
        },
        "modelUsage": {},
    }
    usages = parse_usage(result, fallback_model="claude-sonnet-5-5")
    assert usages == [
        Usage("claude-sonnet-5-5", 100, 200, 100, 1000, 50, reported_cost=0.01),
    ]
    with pytest.raises(PricingError, match="fallback"):
        parse_usage(result)


def test_top_level_cache_creation_without_split_counts_as_five_minutes() -> None:
    result = {"usage": {"cache_creation_input_tokens": 300}, "modelUsage": {}}
    assert parse_usage(result, "claude-sonnet-5-5")[0].cache_write_5m == 300


def test_empty_result_has_no_usage() -> None:
    result = {"usage": {"input_tokens": 0, "output_tokens": 0}, "modelUsage": {}}
    assert parse_usage(result) == []


def test_malformed_model_usage_entry_is_rejected() -> None:
    with pytest.raises(PricingError, match="inputTokens"):
        parse_usage({"modelUsage": {"claude-sonnet-5-5": {"outputTokens": 1}}})


def test_cache_read_ratio_and_token_totals() -> None:
    usages = [
        Usage("m", input=100, cache_write_5m=200, cache_write_1h=100, cache_read=600, output=50)
    ]
    assert cache_read_ratio(usages) == pytest.approx(0.6)
    assert total_tokens(usages) == 1050
    assert cache_read_ratio([]) is None


def test_pricing_loads_a_custom_table(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "unit_tokens": 1000,
                "models": {
                    "m": {
                        "input": 1,
                        "cache_write_5m": 1,
                        "cache_write_1h": 1,
                        "cache_read": 1,
                        "output": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert Pricing.load(path).cost(Usage("m", input=500, output=500)) == pytest.approx(1.5)
