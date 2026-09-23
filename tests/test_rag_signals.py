"""Tests for src/observability/rag_signals.py (Phase B4).

All I/O is patched out — log_analytics is monkeypatched to collect records.
No real LLM calls, no file writes.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from src.observability.rag_signals import record_rag_signals, _safe_stats, _eval_summary


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

class TestSafeStats:
    def test_empty_returns_none(self):
        assert _safe_stats([]) is None

    def test_single_value(self):
        result = _safe_stats([0.8])
        assert result["min"] == result["max"] == result["mean"] == 0.8
        assert result["count"] == 1

    def test_multiple_values(self):
        result = _safe_stats([0.6, 0.8, 1.0])
        assert result["min"] == 0.6
        assert result["max"] == 1.0
        assert result["mean"] == pytest.approx(0.8)
        assert result["count"] == 3

    def test_rounds_to_4dp(self):
        result = _safe_stats([1/3, 2/3])
        assert len(str(result["mean"]).split(".")[-1]) <= 4


class TestEvalSummary:
    def test_empty_metrics_returns_none_pass_rate(self):
        result = _eval_summary({})
        assert result["pass_rate"] is None

    def test_all_pass(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "answer_relevancy": {"score": 0.85, "passed": True},
        }
        result = _eval_summary(metrics)
        assert result["pass_rate"] == 1.0
        assert result["metrics"]["faithfulness"] == "pass"
        assert result["metrics"]["answer_relevancy"] == "pass"

    def test_partial_pass(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "answer_relevancy": {"score": 0.5, "passed": False},
        }
        result = _eval_summary(metrics)
        assert result["pass_rate"] == pytest.approx(0.5)

    def test_skipped_not_counted_in_pass_rate(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "contextual_precision": {"status": "skipped", "reason": "no expected_output"},
        }
        result = _eval_summary(metrics)
        assert result["pass_rate"] == 1.0
        assert result["metrics"]["contextual_precision"] == "skipped"

    def test_error_not_counted_in_pass_rate(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "toxicity": {"error": "LLM unavailable"},
        }
        result = _eval_summary(metrics)
        assert result["pass_rate"] == 1.0
        assert result["metrics"]["toxicity"] == "error"

    def test_all_skipped_returns_none_pass_rate(self):
        metrics = {
            "contextual_precision": {"status": "skipped"},
            "contextual_recall": {"status": "skipped"},
        }
        result = _eval_summary(metrics)
        assert result["pass_rate"] is None


# ---------------------------------------------------------------------------
# Integration tests for record_rag_signals
# ---------------------------------------------------------------------------

class TestRecordRagSignals:
    def _call(self, **kwargs):
        """Call with log_analytics patched to capture records."""
        captured = []
        with patch("src.rag.analytics.log_analytics", side_effect=captured.append):
            record = record_rag_signals(**kwargs)
        return record, captured

    def test_minimal_call_emits_record(self):
        record, captured = self._call(tenant="user-1", strategy="adaptive")
        assert record["type"] == "rag_quality"
        assert record["tenant"] == "user-1"
        assert record["strategy"] == "adaptive"
        assert len(captured) == 1

    def test_retrieval_scores_populated(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="adaptive",
            context_count=3,
            dense_scores=[0.7, 0.8, 0.9],
        )
        assert record["retrieval"]["context_count"] == 3
        assert record["retrieval"]["dense_scores"]["count"] == 3
        assert record["retrieval"]["dense_scores"]["min"] == 0.7

    def test_reranker_scores_populated(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="corrective",
            reranker_scores=[0.6, 0.85],
        )
        assert "reranker_scores" in record["retrieval"]
        assert record["retrieval"]["reranker_scores"]["max"] == 0.85

    def test_eval_pass_rate_computed(self):
        metrics = {
            "faithfulness": {"score": 0.9, "passed": True},
            "answer_relevancy": {"score": 0.4, "passed": False},
        }
        record, _ = self._call(
            tenant="user-2",
            strategy="cache",
            eval_metrics=metrics,
        )
        assert record["eval"]["pass_rate"] == pytest.approx(0.5)

    def test_guard_fields_included(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="adaptive",
            guard_retries=1,
            guard_outcome="refined",
        )
        assert record["guard"]["retries"] == 1
        assert record["guard"]["outcome"] == "refined"

    def test_cost_rounded(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="adaptive",
            estimated_cost_usd=0.000123456789,
        )
        assert len(str(record["estimated_cost_usd"]).split(".")[-1]) <= 6

    def test_missing_optional_fields_absent_from_record(self):
        record, _ = self._call(tenant="user-1", strategy="adaptive")
        assert "retrieval" not in record
        assert "eval" not in record
        assert "guard" not in record
        assert "estimated_cost_usd" not in record

    def test_empty_dense_scores_omits_retrieval_block(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="adaptive",
            dense_scores=[],
        )
        assert "retrieval" not in record

    def test_non_numeric_scores_filtered(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="adaptive",
            dense_scores=["bad", 0.7, None, 0.9],
        )
        assert record["retrieval"]["dense_scores"]["count"] == 2

    def test_exception_in_log_analytics_returns_empty_dict(self):
        with patch("src.rag.analytics.log_analytics", side_effect=RuntimeError("disk full")):
            result = record_rag_signals(tenant="user-1", strategy="adaptive")
        assert result == {}

    def test_ts_is_numeric(self):
        record, _ = self._call(tenant="user-1", strategy="adaptive")
        assert isinstance(record["ts"], float)
        assert record["ts"] > 0

    def test_latency_ms_included(self):
        record, _ = self._call(
            tenant="user-1",
            strategy="adaptive",
            latency_ms=342.7,
        )
        assert record["latency_ms"] == 342.7
