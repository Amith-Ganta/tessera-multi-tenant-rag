"""Tests for src/observability/cost.py (Phase B1).

Verifies:
1. estimate_usd returns correct values for known models.
2. Unknown model returns 0.0 (no KeyError).
3. record() accumulates correctly.
4. spend_so_far() reflects accumulated value.
5. reset() zeroes the accumulator.
6. over_cap() triggers correctly.
7. RATES contains deepseek-flash as the default model.
"""
from __future__ import annotations

import os
import pytest


class TestEstimateUsd:
    def setup_method(self):
        from src.observability.cost import reset
        reset()

    def test_deepseek_flash_cost(self):
        from src.observability.cost import estimate_usd
        # 1M tokens at 0.25 = $0.25
        result = estimate_usd("deepseek/deepseek-flash", 500_000, 500_000)
        assert abs(result - 0.25) < 1e-9

    def test_gpt4o_mini_cost(self):
        from src.observability.cost import estimate_usd
        result = estimate_usd("openai/gpt-4o-mini", 1_000_000, 0)
        assert abs(result - 0.15) < 1e-9

    def test_unknown_model_returns_zero(self):
        from src.observability.cost import estimate_usd
        result = estimate_usd("unknown/model-xyz", 1_000_000, 0)
        assert result == 0.0

    def test_zero_tokens_returns_zero(self):
        from src.observability.cost import estimate_usd
        result = estimate_usd("deepseek/deepseek-flash", 0, 0)
        assert result == 0.0


class TestAccumulator:
    def setup_method(self):
        from src.observability.cost import reset
        reset()

    def test_record_accumulates(self):
        from src.observability.cost import record, spend_so_far
        record(0.01)
        record(0.02)
        assert abs(spend_so_far() - 0.03) < 1e-12

    def test_reset_zeroes(self):
        from src.observability.cost import record, reset, spend_so_far
        record(5.0)
        reset()
        assert spend_so_far() == 0.0

    def test_negative_ignored(self):
        from src.observability.cost import record, spend_so_far
        record(-10.0)
        assert spend_so_far() == 0.0


class TestCapGuard:
    def setup_method(self):
        from src.observability.cost import reset
        reset()

    def test_over_cap_when_exceeded(self, monkeypatch):
        monkeypatch.setenv("TESSERA_DAILY_SPEND_USD_CAP", "1.0")
        from src.observability.cost import record, over_cap
        record(1.01)
        assert over_cap() is True

    def test_not_over_cap_when_under(self, monkeypatch):
        monkeypatch.setenv("TESSERA_DAILY_SPEND_USD_CAP", "1.0")
        from src.observability.cost import record, over_cap
        record(0.99)
        assert over_cap() is False

    def test_disabled_when_cap_zero(self, monkeypatch):
        monkeypatch.setenv("TESSERA_DAILY_SPEND_USD_CAP", "0")
        from src.observability.cost import record, over_cap
        record(999.0)
        assert over_cap() is False


class TestRatesTable:
    def test_deepseek_flash_present(self):
        from src.observability.cost import RATES
        assert "deepseek/deepseek-flash" in RATES

    def test_no_wildcard_zero_rate_for_supported_models(self):
        from src.observability.cost import RATES
        for model, rate in RATES.items():
            assert rate > 0, f"model {model} has zero rate"
