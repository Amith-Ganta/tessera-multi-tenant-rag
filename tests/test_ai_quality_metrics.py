"""Item 21: AI quality metrics tests.

Covers:
- _safe_stats() correctness: empty list, single item, multiple items
- _eval_summary() pass_rate computation, metric status classification
- record_rag_signals() assembles and emits correct record structure
- Required fields always present: type, ts, tenant, strategy
- Optional fields absent when not supplied
- Retrieval stats computed correctly from scores
- Guard block included only when retries/outcome supplied
- estimated_cost_usd included and rounded
- Never raises — exceptions swallowed and {} returned
- Analytics log receives the assembled record

No real I/O or network calls are made.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

# Stub langchain_chroma before any src.rag imports.
_stub_chroma = types.ModuleType("langchain_chroma")
_stub_chroma.Chroma = MagicMock()
sys.modules.setdefault("langchain_chroma", _stub_chroma)


class TestSafeStats:
    """_safe_stats() — pure stats helper."""

    def _call(self, scores):
        from src.observability.rag_signals import _safe_stats
        return _safe_stats(scores)

    def test_empty_list_returns_none(self):
        assert self._call([]) is None

    def test_single_item_min_max_mean_equal(self):
        result = self._call([0.5])
        assert result is not None
        assert result["min"] == result["max"] == result["mean"] == 0.5

    def test_multiple_items_min_max_mean(self):
        result = self._call([0.2, 0.8, 0.5])
        assert result["min"] == 0.2
        assert result["max"] == 0.8
        assert abs(result["mean"] - 0.5) < 0.001

    def test_count_matches_input_length(self):
        result = self._call([0.1, 0.2, 0.3, 0.4])
        assert result["count"] == 4

    def test_values_rounded_to_4_decimals(self):
        result = self._call([1 / 3])
        assert result["mean"] == round(1 / 3, 4)

    def test_returns_all_required_keys(self):
        result = self._call([0.5, 0.7])
        assert set(result.keys()) >= {"min", "max", "mean", "count"}


class TestEvalSummary:
    """_eval_summary() — pass_rate and per-metric status classification."""

    def _call(self, metrics):
        from src.observability.rag_signals import _eval_summary
        return _eval_summary(metrics)

    def test_empty_metrics_returns_none_pass_rate(self):
        result = self._call({})
        assert result["pass_rate"] is None

    def test_all_passing_metrics_pass_rate_is_1(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "relevancy": {"score": 0.8, "passed": True},
        }
        result = self._call(metrics)
        assert result["pass_rate"] == 1.0

    def test_all_failing_metrics_pass_rate_is_0(self):
        metrics = {
            "faithfulness": {"score": 0.1, "passed": False},
            "relevancy": {"score": 0.2, "passed": False},
        }
        result = self._call(metrics)
        assert result["pass_rate"] == 0.0

    def test_mixed_pass_fail_pass_rate(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "relevancy": {"score": 0.1, "passed": False},
        }
        result = self._call(metrics)
        assert result["pass_rate"] == 0.5

    def test_error_metric_excluded_from_pass_rate(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "bad_metric": {"error": "timeout"},
        }
        result = self._call(metrics)
        # Only faithfulness is numeric; bad_metric is error
        assert result["pass_rate"] == 1.0

    def test_skipped_metric_excluded_from_pass_rate(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "skipped_metric": {"status": "skipped"},
        }
        result = self._call(metrics)
        assert result["pass_rate"] == 1.0

    def test_metric_status_pass_correctly_classified(self):
        metrics = {"faithfulness": {"score": 0.9, "passed": True}}
        result = self._call(metrics)
        assert result["metrics"]["faithfulness"] == "pass"

    def test_metric_status_fail_correctly_classified(self):
        metrics = {"faithfulness": {"score": 0.1, "passed": False}}
        result = self._call(metrics)
        assert result["metrics"]["faithfulness"] == "fail"

    def test_metric_status_error_correctly_classified(self):
        metrics = {"faithfulness": {"error": "api_timeout"}}
        result = self._call(metrics)
        assert result["metrics"]["faithfulness"] == "error"

    def test_metric_status_skipped_correctly_classified(self):
        metrics = {"faithfulness": {"status": "skipped"}}
        result = self._call(metrics)
        assert result["metrics"]["faithfulness"] == "skipped"


class TestRecordRagSignals:
    """record_rag_signals() assembles and emits structured quality records."""

    def _record(self, **kwargs):
        from src.observability.rag_signals import record_rag_signals
        captured = []
        with patch("src.rag.analytics.log_analytics", captured.append):
            result = record_rag_signals(**kwargs)
        return result, captured

    def test_required_fields_always_present(self):
        result, _ = self._record(tenant="acme", strategy="adaptive")
        assert result["type"] == "rag_quality"
        assert result["tenant"] == "acme"
        assert result["strategy"] == "adaptive"
        assert "ts" in result

    def test_emits_to_analytics_log(self):
        _, captured = self._record(tenant="acme", strategy="adaptive")
        assert len(captured) == 1

    def test_analytics_record_matches_return_value(self):
        result, captured = self._record(tenant="acme", strategy="adaptive")
        assert captured[0] == result

    def test_latency_ms_included_when_supplied(self):
        result, _ = self._record(tenant="t", strategy="s", latency_ms=123.456)
        assert result["latency_ms"] == 123.5

    def test_latency_ms_absent_when_not_supplied(self):
        result, _ = self._record(tenant="t", strategy="s")
        assert "latency_ms" not in result

    def test_context_count_in_retrieval_block(self):
        result, _ = self._record(tenant="t", strategy="s", context_count=5)
        assert result["retrieval"]["context_count"] == 5

    def test_dense_scores_stats_in_retrieval_block(self):
        result, _ = self._record(tenant="t", strategy="s", dense_scores=[0.8, 0.6, 0.7])
        assert "dense_scores" in result["retrieval"]
        assert result["retrieval"]["dense_scores"]["count"] == 3

    def test_empty_dense_scores_omitted(self):
        result, _ = self._record(tenant="t", strategy="s", dense_scores=[])
        assert "retrieval" not in result or "dense_scores" not in result.get("retrieval", {})

    def test_guard_block_included_when_retries_supplied(self):
        result, _ = self._record(tenant="t", strategy="s", guard_retries=2)
        assert result["guard"]["retries"] == 2

    def test_guard_block_included_when_outcome_supplied(self):
        result, _ = self._record(tenant="t", strategy="s", guard_outcome="passed")
        assert result["guard"]["outcome"] == "passed"

    def test_guard_block_absent_when_not_supplied(self):
        result, _ = self._record(tenant="t", strategy="s")
        assert "guard" not in result

    def test_estimated_cost_included_and_rounded(self):
        result, _ = self._record(tenant="t", strategy="s", estimated_cost_usd=0.00123456)
        assert "estimated_cost_usd" in result
        assert result["estimated_cost_usd"] == round(0.00123456, 6)

    def test_extra_dict_included_verbatim(self):
        result, _ = self._record(tenant="t", strategy="s", extra={"my_key": "my_val"})
        assert result["extra"]["my_key"] == "my_val"

    def test_never_raises_on_bad_input(self):
        from src.observability.rag_signals import record_rag_signals
        # Pass non-numeric scores — must not raise
        result = record_rag_signals(tenant="t", strategy="s", dense_scores=["bad", None, 0.5])
        assert isinstance(result, dict)

    def test_eval_block_included_when_metrics_supplied(self):
        metrics = {"faithfulness": {"score": 0.9, "passed": True}}
        result, _ = self._record(tenant="t", strategy="s", eval_metrics=metrics)
        assert "eval" in result
        assert result["eval"]["pass_rate"] == 1.0
