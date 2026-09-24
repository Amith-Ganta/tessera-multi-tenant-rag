"""Item 22: AI capacity model tests.

Covers:
- RATES dict has expected model keys and non-negative values
- estimate_usd() returns zero for unknown model (safe default)
- estimate_usd() correctness for known models
- estimate_usd() returns 0 for zero tokens
- record() accumulates spend and returns new total
- spend_so_far() reflects accumulated value after record()
- reset() zeroes the accumulator
- reset() is idempotent
- daily_cap() reads from env var (default 5.0 USD)
- daily_cap() returns 5.0 when env var is malformed
- over_cap() False when cap disabled (0)
- over_cap() False when spend below cap
- over_cap() True when spend exceeds cap
- Thread safety: record() from multiple threads is consistent

No real network or filesystem calls are made.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import patch


class TestRatesTable:
    """RATES dict must contain the expected models with sane values."""

    def test_rates_contains_deepseek_flash(self):
        from src.observability.cost import RATES
        assert "deepseek/deepseek-flash" in RATES

    def test_rates_contains_deepseek_chat(self):
        from src.observability.cost import RATES
        assert "deepseek/deepseek-chat" in RATES

    def test_rates_contains_gpt4o_mini(self):
        from src.observability.cost import RATES
        assert "openai/gpt-4o-mini" in RATES

    def test_rates_contains_gpt4o(self):
        from src.observability.cost import RATES
        assert "openai/gpt-4o" in RATES

    def test_all_rates_non_negative(self):
        from src.observability.cost import RATES
        for model, rate in RATES.items():
            assert rate >= 0.0, f"{model} has negative rate {rate}"

    def test_deepseek_flash_cheaper_than_chat(self):
        from src.observability.cost import RATES
        assert RATES["deepseek/deepseek-flash"] <= RATES["deepseek/deepseek-chat"]

    def test_gpt4o_more_expensive_than_gpt4o_mini(self):
        from src.observability.cost import RATES
        assert RATES["openai/gpt-4o"] > RATES["openai/gpt-4o-mini"]


class TestEstimateUsd:
    """estimate_usd() cost calculations."""

    def test_unknown_model_returns_zero(self):
        from src.observability.cost import estimate_usd
        assert estimate_usd("totally/unknown-model", 1000, 200) == 0.0

    def test_zero_tokens_returns_zero(self):
        from src.observability.cost import estimate_usd
        assert estimate_usd("deepseek/deepseek-flash", 0, 0) == 0.0

    def test_cost_proportional_to_token_count(self):
        from src.observability.cost import estimate_usd
        cost1 = estimate_usd("deepseek/deepseek-flash", 1000, 0)
        cost2 = estimate_usd("deepseek/deepseek-flash", 2000, 0)
        assert abs(cost2 - 2 * cost1) < 1e-10

    def test_prompt_and_completion_tokens_both_counted(self):
        from src.observability.cost import estimate_usd
        cost_prompt_only = estimate_usd("deepseek/deepseek-flash", 1000, 0)
        cost_completion_only = estimate_usd("deepseek/deepseek-flash", 0, 1000)
        cost_combined = estimate_usd("deepseek/deepseek-flash", 1000, 1000)
        assert abs(cost_combined - cost_prompt_only - cost_completion_only) < 1e-10

    def test_result_is_float(self):
        from src.observability.cost import estimate_usd
        result = estimate_usd("openai/gpt-4o-mini", 500, 100)
        assert isinstance(result, float)

    def test_positive_cost_for_known_model(self):
        from src.observability.cost import estimate_usd
        cost = estimate_usd("openai/gpt-4o-mini", 1000, 200)
        assert cost > 0


class TestSpendAccumulator:
    """record(), spend_so_far(), reset() — in-process accumulator."""

    def setup_method(self):
        from src.observability.cost import reset
        reset()

    def test_initial_spend_is_zero_after_reset(self):
        from src.observability.cost import spend_so_far
        assert spend_so_far() == 0.0

    def test_record_adds_to_accumulator(self):
        from src.observability.cost import record, spend_so_far
        record(0.01)
        assert abs(spend_so_far() - 0.01) < 1e-9

    def test_record_returns_new_total(self):
        from src.observability.cost import record
        total = record(0.05)
        assert abs(total - 0.05) < 1e-9

    def test_multiple_records_accumulate(self):
        from src.observability.cost import record, spend_so_far
        record(0.01)
        record(0.02)
        record(0.03)
        assert abs(spend_so_far() - 0.06) < 1e-9

    def test_reset_zeroes_accumulator(self):
        from src.observability.cost import record, spend_so_far, reset
        record(1.0)
        reset()
        assert spend_so_far() == 0.0

    def test_reset_is_idempotent(self):
        from src.observability.cost import reset, spend_so_far
        reset()
        reset()
        assert spend_so_far() == 0.0

    def test_negative_amount_clamped_to_zero(self):
        """record() with negative amount must not decrease the accumulator."""
        from src.observability.cost import record, spend_so_far
        record(0.10)
        record(-0.05)
        assert abs(spend_so_far() - 0.10) < 1e-9

    def test_thread_safety_concurrent_records(self):
        """Concurrent record() calls must not lose increments."""
        from src.observability.cost import record, spend_so_far, reset
        reset()
        n_threads = 20
        amount = 0.001
        threads = [threading.Thread(target=record, args=(amount,)) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        expected = n_threads * amount
        assert abs(spend_so_far() - expected) < 1e-9


class TestDailyCapAndOverCap:
    """daily_cap() and over_cap() — budget enforcement."""

    def setup_method(self):
        from src.observability.cost import reset
        reset()

    def test_default_cap_is_5_usd(self):
        from src.observability.cost import daily_cap
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TESSERA_DAILY_SPEND_USD_CAP", None)
            cap = daily_cap()
        assert cap == 5.0

    def test_cap_reads_from_env_var(self):
        from src.observability.cost import daily_cap
        with patch.dict(os.environ, {"TESSERA_DAILY_SPEND_USD_CAP": "10.0"}):
            assert daily_cap() == 10.0

    def test_malformed_env_var_falls_back_to_default(self):
        from src.observability.cost import daily_cap
        with patch.dict(os.environ, {"TESSERA_DAILY_SPEND_USD_CAP": "not-a-number"}):
            assert daily_cap() == 5.0

    def test_over_cap_false_when_cap_disabled(self):
        from src.observability.cost import over_cap, record
        record(1000.0)
        with patch.dict(os.environ, {"TESSERA_DAILY_SPEND_USD_CAP": "0"}):
            assert over_cap() is False

    def test_over_cap_false_when_below_cap(self):
        from src.observability.cost import over_cap, record
        record(1.0)
        with patch.dict(os.environ, {"TESSERA_DAILY_SPEND_USD_CAP": "5.0"}):
            assert over_cap() is False

    def test_over_cap_true_when_above_cap(self):
        from src.observability.cost import over_cap, record
        record(6.0)
        with patch.dict(os.environ, {"TESSERA_DAILY_SPEND_USD_CAP": "5.0"}):
            assert over_cap() is True
