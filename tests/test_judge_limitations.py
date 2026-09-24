"""Item 12: Judge and eval limitation edge case tests.

Tests cover:
- Gate boundary: exactly-at-threshold values (pass vs. fail)
- Gate edge: empty report, null aggregates, non-numeric values
- Gate edge: all metrics skipped → still passes (skip ≠ fail)
- Judge quality signal: silently no-ops on exception in log_analytics
- Judge quality signal: only emits for _QUALITY_SIGNAL_METRICS
- Judge quality signal: no emission when all metrics pass
- submit_judge: returns "unavailable" when Redis publish fails (MM-02 fix)
- submit_judge: returns "pending" when Redis publish succeeds

No real LLM/Redis calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from evals.gate import run_gate


# ---------------------------------------------------------------------------
# Gate: boundary / exact-threshold behaviour
# ---------------------------------------------------------------------------

def _full_report(
    relevancy: float,
    correctness: float,
    latency: float,
    error_rate: float,
    cost: float,
) -> dict:
    return {
        "aggregates": {"mean_relevancy": relevancy, "mean_correctness": correctness},
        "performance": {
            "latency_p95_ms": latency,
            "error_rate": error_rate,
            "cost_per_request_usd": cost,
        },
    }


class TestGateBoundaryValues:
    def test_exactly_at_relevancy_floor_passes(self):
        from src.rag.config import MIN_MEAN_RELEVANCY
        r = run_gate(_full_report(MIN_MEAN_RELEVANCY, 0.8, 1000, 0.01, 0.001))
        rcheck = next(c for c in r["checks"] if c["name"] == "mean_relevancy")
        assert rcheck["passed"] is True

    def test_one_ulp_below_relevancy_floor_fails(self):
        from src.rag.config import MIN_MEAN_RELEVANCY
        val = MIN_MEAN_RELEVANCY - 0.001
        r = run_gate(_full_report(val, 0.8, 1000, 0.01, 0.001))
        rcheck = next(c for c in r["checks"] if c["name"] == "mean_relevancy")
        assert rcheck["passed"] is False

    def test_exactly_at_correctness_floor_passes(self):
        from src.rag.config import MIN_MEAN_CORRECTNESS
        r = run_gate(_full_report(0.9, MIN_MEAN_CORRECTNESS, 1000, 0.01, 0.001))
        ccheck = next(c for c in r["checks"] if c["name"] == "mean_correctness")
        assert ccheck["passed"] is True

    def test_exactly_at_latency_ceiling_passes(self):
        from src.rag.config import LATENCY_P95_MAX_MS
        r = run_gate(_full_report(0.9, 0.7, LATENCY_P95_MAX_MS, 0.01, 0.001))
        lcheck = next(c for c in r["checks"] if c["name"] == "latency_p95_ms")
        assert lcheck["passed"] is True

    def test_one_ms_above_latency_ceiling_fails(self):
        from src.rag.config import LATENCY_P95_MAX_MS
        r = run_gate(_full_report(0.9, 0.7, LATENCY_P95_MAX_MS + 1, 0.01, 0.001))
        lcheck = next(c for c in r["checks"] if c["name"] == "latency_p95_ms")
        assert lcheck["passed"] is False

    def test_exactly_at_error_rate_ceiling_passes(self):
        from src.rag.config import ERROR_RATE_MAX
        r = run_gate(_full_report(0.9, 0.7, 1000, ERROR_RATE_MAX, 0.001))
        echeck = next(c for c in r["checks"] if c["name"] == "error_rate")
        assert echeck["passed"] is True

    def test_exactly_at_cost_ceiling_passes(self):
        from src.rag.config import COST_PER_REQUEST_MAX_USD
        r = run_gate(_full_report(0.9, 0.7, 1000, 0.01, COST_PER_REQUEST_MAX_USD))
        ccheck = next(c for c in r["checks"] if c["name"] == "cost_per_request_usd")
        assert ccheck["passed"] is True


class TestGateEdgeCases:
    def test_empty_report_skips_all_and_passes(self):
        """Empty report: all five checks skipped → gate passes (skip ≠ fail)."""
        r = run_gate({})
        assert r["passed"] is True
        assert all(c["skipped"] for c in r["checks"])

    def test_null_aggregates_skips_those_checks(self):
        r = run_gate({"aggregates": None, "performance": {"latency_p95_ms": 500}})
        rel = next(c for c in r["checks"] if c["name"] == "mean_relevancy")
        assert rel["skipped"] is True

    def test_non_numeric_value_is_treated_as_missing_and_skipped(self):
        r = run_gate({
            "aggregates": {"mean_relevancy": "not-a-number"},
            "performance": {},
        })
        rel = next(c for c in r["checks"] if c["name"] == "mean_relevancy")
        assert rel["skipped"] is True

    def test_single_failing_metric_fails_gate(self):
        r = run_gate(_full_report(0.9, 0.7, 1000, 0.99, 0.001))
        assert r["passed"] is False

    def test_skipped_checks_do_not_count_as_failures(self):
        """A report with only correctness present: correctness check passes; rest skipped."""
        r = run_gate({"aggregates": {"mean_correctness": 0.9}, "performance": {}})
        correct_check = next(c for c in r["checks"] if c["name"] == "mean_correctness")
        assert correct_check["passed"] is True
        assert r["passed"] is True


# ---------------------------------------------------------------------------
# Judge quality signal edge cases
# ---------------------------------------------------------------------------

class TestJudgeQualitySignal:
    def _emit(self, result: dict) -> None:
        from src.judge.async_runner import _emit_judge_quality_signal
        _emit_judge_quality_signal("trace-test", result)

    def test_no_exception_when_metrics_empty(self):
        self._emit({"metrics": {}})  # should not raise

    def test_no_exception_when_metrics_absent(self):
        self._emit({})  # should not raise

    def test_no_emission_when_all_metrics_pass(self):
        captured = []
        with patch("src.rag.analytics.log_analytics", side_effect=captured.append):
            self._emit({"metrics": {
                "faithfulness": {"score": 0.9, "passed": True, "threshold": 0.7},
                "answer_relevancy": {"score": 0.8, "passed": True, "threshold": 0.6},
            }})
        assert not captured, "log_analytics must NOT be called when all metrics pass"

    def test_emission_when_faithfulness_fails(self):
        captured = []
        with patch("src.rag.analytics.log_analytics", side_effect=captured.append):
            self._emit({"metrics": {
                "faithfulness": {"score": 0.2, "passed": False, "threshold": 0.7, "reason": "low"},
                "answer_relevancy": {"score": 0.9, "passed": True, "threshold": 0.6},
            }})
        assert len(captured) == 1
        event = captured[0]
        assert event["event"] == "judge_quality_fail"
        assert "faithfulness" in event["failed_metrics"]
        assert "answer_relevancy" not in event["failed_metrics"]

    def test_emission_when_correctness_fails(self):
        captured = []
        with patch("src.rag.analytics.log_analytics", side_effect=captured.append):
            self._emit({"metrics": {
                "correctness": {"score": 0.1, "passed": False, "threshold": 0.5},
            }})
        assert len(captured) == 1

    def test_non_quality_metrics_not_emitted(self):
        """Metrics not in _QUALITY_SIGNAL_METRICS must not appear in the event."""
        captured = []
        with patch("src.rag.analytics.log_analytics", side_effect=captured.append):
            self._emit({"metrics": {
                "faithfulness": {"score": 0.1, "passed": False, "threshold": 0.7},
                "latency_ms": {"score": 500, "passed": False, "threshold": 300},
            }})
        assert captured
        assert "latency_ms" not in captured[0]["failed_metrics"]

    def test_no_exception_when_log_analytics_raises(self):
        """_emit_judge_quality_signal must swallow exceptions from log_analytics."""
        with patch("src.rag.analytics.log_analytics", side_effect=RuntimeError("boom")):
            self._emit({"metrics": {
                "faithfulness": {"score": 0.1, "passed": False, "threshold": 0.7},
            }})
        # must not raise


# ---------------------------------------------------------------------------
# submit_judge returns honest status on queue failure (MM-02 regression)
# ---------------------------------------------------------------------------

class TestSubmitJudgeStatus:
    """submit_judge uses `from .redis_queue import judge_queue` inside the function,
    so we patch at the redis_queue module level (where the object lives)."""

    def _call_submit(self, mock_publish_result: bool) -> dict:
        """Helper: call submit_judge with JUDGE_QUEUE_ENABLED=True and a mocked queue."""
        import src.rag.config as cfg
        from src.judge.async_runner import submit_judge
        import src.judge.redis_queue as rq_mod

        orig_enabled = cfg.JUDGE_QUEUE_ENABLED
        orig_jq = rq_mod.judge_queue

        mock_q = MagicMock()
        mock_q.publish.return_value = mock_publish_result
        mock_store = MagicMock()
        mock_store.set_pending = MagicMock()

        cfg.JUDGE_QUEUE_ENABLED = True
        rq_mod.judge_queue = mock_q
        import src.judge.async_runner as ar_mod
        orig_store = ar_mod.judge_store
        ar_mod.judge_store = mock_store
        try:
            return submit_judge(
                trace_id="t-test",
                question="test?",
                answer="answer",
                contexts=["ctx"],
                evaluate_fn=lambda q, a, c: {},
            )
        finally:
            cfg.JUDGE_QUEUE_ENABLED = orig_enabled
            rq_mod.judge_queue = orig_jq
            ar_mod.judge_store = orig_store

    def test_returns_unavailable_when_publish_fails(self):
        result = self._call_submit(False)
        assert result["status"] == "unavailable"
        assert result.get("reason") == "queue_unavailable"

    def test_returns_pending_when_publish_succeeds(self):
        result = self._call_submit(True)
        assert result["status"] == "pending"
        assert result.get("trace_id") == "t-test"
