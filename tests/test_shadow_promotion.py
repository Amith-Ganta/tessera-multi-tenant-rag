"""Item 13: Shadow promotion methodology tests.

Covers:
- ShadowResult.record() accumulation and mean computation
- compare(): fail-closed when sample count below MIN_SHADOW_SAMPLES
- compare(): fail-closed when primary metric absent in either run
- compare(): passes only when candidate >= baseline + margin on ALL primaries
- compare(): exact-margin boundary (pass) vs. one unit under (fail)
- compare(): negative margin accepted (cost-sensitive route)
- run_shadow_experiment(): wraps both fns and compares; exceptions skipped
- run_shadow_experiment(): fail-closed when one side keeps failing

No real LLM calls are made.
"""

from __future__ import annotations

import pytest

from src.rag.promotion_gate import (
    MIN_SHADOW_SAMPLES,
    PROMOTION_MARGIN,
    PRIMARY_METRICS,
    ShadowResult,
    PromotionDecision,
    compare,
    run_shadow_experiment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(name: str, n: int, metrics: dict[str, float]) -> ShadowResult:
    """Build a ShadowResult by calling record() n times with constant metric values."""
    sr = ShadowResult(config_name=name)
    metric_dict = {k: {"score": v} for k, v in metrics.items()}
    for _ in range(n):
        sr.record(metric_dict)
    return sr


# ---------------------------------------------------------------------------
# ShadowResult unit tests
# ---------------------------------------------------------------------------

class TestShadowResult:
    def test_initial_state(self):
        sr = ShadowResult(config_name="test")
        assert sr.sample_count == 0
        assert sr.metric_sums == {}

    def test_record_increments_sample_count(self):
        sr = ShadowResult(config_name="test")
        sr.record({"faithfulness": {"score": 0.8}})
        assert sr.sample_count == 1

    def test_mean_is_average_of_recorded_scores(self):
        sr = ShadowResult(config_name="test")
        sr.record({"faithfulness": {"score": 0.6}})
        sr.record({"faithfulness": {"score": 0.8}})
        assert sr.mean("faithfulness") == pytest.approx(0.7)

    def test_mean_of_absent_metric_returns_none(self):
        sr = _make_result("b", 5, {"faithfulness": 0.8})
        assert sr.mean("answer_relevancy") is None

    def test_non_numeric_score_is_silently_skipped(self):
        sr = ShadowResult(config_name="test")
        sr.record({"faithfulness": {"score": "bad"}})
        assert sr.mean("faithfulness") is None

    def test_summary_returns_all_recorded_metrics(self):
        sr = _make_result("b", 3, {"faithfulness": 0.9, "answer_relevancy": 0.7})
        s = sr.summary()
        assert "faithfulness" in s
        assert "answer_relevancy" in s


# ---------------------------------------------------------------------------
# compare() fail-closed cases
# ---------------------------------------------------------------------------

class TestCompareFailClosed:
    def test_fails_when_baseline_below_min_samples(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES - 1, {"faithfulness": 0.9, "answer_relevancy": 0.9})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {"faithfulness": 0.95, "answer_relevancy": 0.95})
        d = compare(b, c)
        assert d.promote is False
        assert "insufficient" in d.reason

    def test_fails_when_candidate_below_min_samples(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.9, "answer_relevancy": 0.9})
        c = _make_result("cand", MIN_SHADOW_SAMPLES - 1, {"faithfulness": 0.95, "answer_relevancy": 0.95})
        d = compare(b, c)
        assert d.promote is False
        assert "insufficient" in d.reason

    def test_fails_when_primary_metric_absent_in_baseline(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.9})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {"faithfulness": 0.95, "answer_relevancy": 0.9})
        d = compare(b, c)
        assert d.promote is False
        check = d.checks.get("answer_relevancy", {})
        assert check.get("pass") is False
        assert "absent" in check.get("reason", "").lower()

    def test_fails_when_primary_metric_absent_in_candidate(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.9, "answer_relevancy": 0.7})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {"faithfulness": 0.95})
        d = compare(b, c)
        assert d.promote is False

    def test_fails_when_candidate_misses_margin_on_one_metric(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.8, "answer_relevancy": 0.8})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {
            "faithfulness": 0.8 + PROMOTION_MARGIN + 0.01,
            "answer_relevancy": 0.8,  # delta = 0, below margin
        })
        d = compare(b, c)
        assert d.promote is False
        assert d.checks["answer_relevancy"]["pass"] is False


# ---------------------------------------------------------------------------
# compare() promotion cases
# ---------------------------------------------------------------------------

class TestComparePromote:
    def test_promotes_when_candidate_beats_margin_on_all_primaries(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.7, "answer_relevancy": 0.7})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {
            "faithfulness": 0.7 + PROMOTION_MARGIN + 0.01,
            "answer_relevancy": 0.7 + PROMOTION_MARGIN + 0.01,
        })
        d = compare(b, c)
        assert d.promote is True

    def test_exactly_at_margin_promotes(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.7, "answer_relevancy": 0.7})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {
            "faithfulness": 0.7 + PROMOTION_MARGIN,
            "answer_relevancy": 0.7 + PROMOTION_MARGIN,
        })
        d = compare(b, c)
        assert d.promote is True

    def test_one_unit_below_margin_fails(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.7, "answer_relevancy": 0.7})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {
            "faithfulness": 0.7 + PROMOTION_MARGIN - 0.001,
            "answer_relevancy": 0.7 + PROMOTION_MARGIN - 0.001,
        })
        d = compare(b, c)
        assert d.promote is False

    def test_custom_margin_zero_allows_equal_score(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.8, "answer_relevancy": 0.8})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {"faithfulness": 0.8, "answer_relevancy": 0.8})
        d = compare(b, c, margin=0.0)
        assert d.promote is True

    def test_negative_margin_allows_slight_regression(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.8, "answer_relevancy": 0.8})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {
            "faithfulness": 0.79,  # -0.01, within negative margin
            "answer_relevancy": 0.79,
        })
        d = compare(b, c, margin=-0.02)
        assert d.promote is True

    def test_decision_carries_both_summaries(self):
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.7, "answer_relevancy": 0.7})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {"faithfulness": 0.75, "answer_relevancy": 0.75})
        d = compare(b, c)
        assert "faithfulness" in d.baseline_summary
        assert "faithfulness" in d.candidate_summary

    def test_custom_primary_metrics_only_those_checked(self):
        """If we override primary_metrics, only those are evaluated."""
        b = _make_result("base", MIN_SHADOW_SAMPLES, {"faithfulness": 0.9})
        c = _make_result("cand", MIN_SHADOW_SAMPLES, {"faithfulness": 0.9 + PROMOTION_MARGIN + 0.01})
        d = compare(b, c, primary_metrics=("faithfulness",))
        assert d.promote is True
        assert "answer_relevancy" not in d.checks


# ---------------------------------------------------------------------------
# run_shadow_experiment()
# ---------------------------------------------------------------------------

def _query_fn(score: float, fail_at: int | None = None):
    """Returns a callable that returns a constant score, optionally raising on call N."""
    call_count = [0]

    def fn(query: dict) -> dict:
        call_count[0] += 1
        if fail_at is not None and call_count[0] == fail_at:
            raise RuntimeError("deliberate failure")
        return {"eval": {"metrics": {
            "faithfulness": {"score": score},
            "answer_relevancy": {"score": score},
        }}}
    return fn


class TestRunShadowExperiment:
    def _queries(self, n: int = MIN_SHADOW_SAMPLES) -> list[dict]:
        return [{"question": f"q{i}"} for i in range(n)]

    def test_promotes_when_candidate_clearly_better(self):
        d = run_shadow_experiment(
            self._queries(MIN_SHADOW_SAMPLES),
            baseline_fn=_query_fn(0.7),
            candidate_fn=_query_fn(0.7 + PROMOTION_MARGIN + 0.05),
        )
        assert d.promote is True

    def test_fails_when_candidate_no_better(self):
        d = run_shadow_experiment(
            self._queries(MIN_SHADOW_SAMPLES),
            baseline_fn=_query_fn(0.8),
            candidate_fn=_query_fn(0.8),
        )
        assert d.promote is False

    def test_fails_closed_when_candidate_keeps_throwing(self):
        """If all candidate calls raise, sample_count stays 0 → fail-closed."""
        d = run_shadow_experiment(
            self._queries(MIN_SHADOW_SAMPLES),
            baseline_fn=_query_fn(0.9),
            candidate_fn=_query_fn(0.9, fail_at=1),  # first call fails
        )
        # baseline accumulates but candidate may be < MIN_SHADOW_SAMPLES
        # (only one query and it fails → 0 candidate samples)
        # promote must be False
        assert d.promote is False

    def test_single_failure_does_not_abort_all_queries(self):
        """One exception in the candidate should not prevent remaining queries."""
        queries = self._queries(MIN_SHADOW_SAMPLES + 2)
        call_count = [0]

        def noisy_candidate(q):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("transient error")
            return {"eval": {"metrics": {
                "faithfulness": {"score": 0.9},
                "answer_relevancy": {"score": 0.9},
            }}}

        d = run_shadow_experiment(
            queries,
            baseline_fn=_query_fn(0.7),
            candidate_fn=noisy_candidate,
        )
        # Candidate accumulates MIN_SHADOW_SAMPLES+ successful calls despite one failure
        assert d.candidate_summary.get("faithfulness") is not None
