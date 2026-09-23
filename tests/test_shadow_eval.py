"""Tests for src/rag/promotion_gate.py — shadow evaluation and promotion gate (Phase B5).

No real LLM calls. All metric scores are injected via lambda functions.
Verifies:
1. compare() promotes when candidate beats baseline by >= margin.
2. compare() rejects when candidate is under the margin.
3. compare() fails-closed when sample count is below MIN_SHADOW_SAMPLES.
4. compare() fails-closed when a primary metric is absent.
5. run_shadow_experiment() accumulates scores across queries.
6. run_shadow_experiment() tolerates exceptions in either function.
7. ShadowResult.mean() returns None for unknown metric.
"""
from __future__ import annotations

import pytest
from src.rag.promotion_gate import (
    ShadowResult,
    PromotionDecision,
    compare,
    run_shadow_experiment,
    MIN_SHADOW_SAMPLES,
    PROMOTION_MARGIN,
    PRIMARY_METRICS,
)


def _make_result(name: str, n: int, faithfulness: float, answer_relevancy: float) -> ShadowResult:
    """Build a ShadowResult with n identical samples for both primary metrics."""
    r = ShadowResult(config_name=name)
    for _ in range(n):
        r.record({
            "faithfulness": {"score": faithfulness},
            "answer_relevancy": {"score": answer_relevancy},
        })
    return r


class TestCompare:
    def test_promotes_when_above_margin(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        candidate = _make_result("cand", MIN_SHADOW_SAMPLES, 0.75, 0.75)
        decision = compare(baseline, candidate, margin=0.02)
        assert decision.promote is True
        assert "candidate" in decision.reason.lower()

    def test_rejects_when_equal_to_baseline(self):
        # Equal scores do not meet margin=0.02
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        candidate = _make_result("cand", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        decision = compare(baseline, candidate, margin=0.02)
        assert decision.promote is False

    def test_rejects_when_below_margin(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        candidate = _make_result("cand", MIN_SHADOW_SAMPLES, 0.71, 0.71)
        decision = compare(baseline, candidate, margin=0.02)
        assert decision.promote is False

    def test_fail_closed_insufficient_baseline_samples(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES - 1, 0.80, 0.80)
        candidate = _make_result("cand", MIN_SHADOW_SAMPLES, 0.90, 0.90)
        decision = compare(baseline, candidate)
        assert decision.promote is False
        assert "insufficient" in decision.reason

    def test_fail_closed_insufficient_candidate_samples(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.80, 0.80)
        candidate = _make_result("cand", 0, 0.90, 0.90)
        decision = compare(baseline, candidate)
        assert decision.promote is False

    def test_fail_closed_missing_metric(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        # Candidate has no faithfulness data
        candidate = ShadowResult(config_name="cand")
        for _ in range(MIN_SHADOW_SAMPLES):
            candidate.record({"answer_relevancy": {"score": 0.90}})
        decision = compare(baseline, candidate)
        assert decision.promote is False
        assert "faithfulness" in decision.checks
        assert decision.checks["faithfulness"]["pass"] is False

    def test_decision_contains_summaries(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.70, 0.72)
        candidate = _make_result("cand", MIN_SHADOW_SAMPLES, 0.80, 0.82)
        decision = compare(baseline, candidate, margin=0.05)
        assert "faithfulness" in decision.baseline_summary
        assert "answer_relevancy" in decision.candidate_summary

    def test_zero_margin_accepts_equal_scores(self):
        baseline = _make_result("base", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        candidate = _make_result("cand", MIN_SHADOW_SAMPLES, 0.70, 0.70)
        decision = compare(baseline, candidate, margin=0.0)
        assert decision.promote is True


class TestRunShadowExperiment:
    def _make_fn(self, faithfulness: float, answer_relevancy: float):
        def fn(query):
            return {
                "answer": "test",
                "eval": {
                    "metrics": {
                        "faithfulness": {"score": faithfulness},
                        "answer_relevancy": {"score": answer_relevancy},
                    }
                }
            }
        return fn

    def test_promotes_better_candidate(self):
        queries = [{"question": f"q{i}"} for i in range(MIN_SHADOW_SAMPLES)]
        decision = run_shadow_experiment(
            queries,
            baseline_fn=self._make_fn(0.70, 0.70),
            candidate_fn=self._make_fn(0.80, 0.80),
        )
        assert decision.promote is True

    def test_rejects_worse_candidate(self):
        queries = [{"question": f"q{i}"} for i in range(MIN_SHADOW_SAMPLES)]
        decision = run_shadow_experiment(
            queries,
            baseline_fn=self._make_fn(0.80, 0.80),
            candidate_fn=self._make_fn(0.70, 0.70),
        )
        assert decision.promote is False

    def test_tolerates_candidate_exception(self):
        call_count = {"n": 0}

        def bad_candidate(query):
            call_count["n"] += 1
            raise RuntimeError("provider down")

        queries = [{"question": f"q{i}"} for i in range(MIN_SHADOW_SAMPLES)]
        decision = run_shadow_experiment(
            queries,
            baseline_fn=self._make_fn(0.80, 0.80),
            candidate_fn=bad_candidate,
        )
        assert decision.promote is False
        assert call_count["n"] == MIN_SHADOW_SAMPLES  # all calls attempted

    def test_insufficient_samples_fail_closed(self):
        queries = [{"question": "single"}]  # below MIN_SHADOW_SAMPLES
        decision = run_shadow_experiment(
            queries,
            baseline_fn=self._make_fn(0.90, 0.90),
            candidate_fn=self._make_fn(0.99, 0.99),
        )
        assert decision.promote is False


class TestShadowResult:
    def test_mean_returns_none_for_unknown_metric(self):
        r = ShadowResult(config_name="x")
        assert r.mean("nonexistent") is None

    def test_ignores_non_numeric_score(self):
        r = ShadowResult(config_name="x")
        r.record({"faithfulness": {"score": "not_a_number"}})
        assert r.mean("faithfulness") is None

    def test_ignores_entry_without_score_key(self):
        r = ShadowResult(config_name="x")
        r.record({"faithfulness": {"error": "metric failed"}})
        assert r.mean("faithfulness") is None
