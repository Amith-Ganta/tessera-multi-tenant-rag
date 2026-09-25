"""Tests for the five-threshold quality gate (Phase 3I / ADR-017).

All tests call evals.gate.run_gate() directly with synthetic report dicts so
no real API calls or disk reads are required.
"""

from __future__ import annotations

import pytest

from evals.gate import run_gate


def _report(
    mean_relevancy: float | None = 0.9,
    mean_correctness: float | None = 0.8,
    latency_p95_ms: float | None = None,
    error_rate: float | None = None,
    cost_per_request_usd: float | None = None,
) -> dict:
    aggregates: dict = {}
    if mean_relevancy is not None:
        aggregates["mean_relevancy"] = mean_relevancy
    if mean_correctness is not None:
        aggregates["mean_correctness"] = mean_correctness
        # gate checks mean_correctness_vector; mirror the value so tests cover
        # the real check name without changing every test's intent
        aggregates["mean_correctness_vector"] = mean_correctness

    perf: dict = {}
    if latency_p95_ms is not None:
        perf["latency_p95_ms"] = latency_p95_ms
    if error_rate is not None:
        perf["error_rate"] = error_rate
    if cost_per_request_usd is not None:
        perf["cost_per_request_usd"] = cost_per_request_usd

    return {"aggregates": aggregates, "performance": perf}


class TestGateStructure:
    def test_result_has_passed_and_checks_keys(self):
        result = run_gate(_report())
        assert "passed" in result
        assert "checks" in result

    def test_result_has_five_checks(self):
        result = run_gate(_report())
        assert len(result["checks"]) == 5

    def test_check_fields_present(self):
        result = run_gate(_report())
        for check in result["checks"]:
            assert "name" in check
            assert "passed" in check
            assert "skipped" in check
            assert "threshold" in check
            assert "op" in check


class TestThresholdLogic:
    def test_passes_when_all_quality_metrics_meet_floor(self):
        result = run_gate(_report(mean_relevancy=0.7, mean_correctness=0.6))
        assert result["passed"] is True

    def test_fails_when_relevancy_below_floor(self):
        result = run_gate(_report(mean_relevancy=0.3, mean_correctness=0.8))
        assert result["passed"] is False
        relevancy_check = next(c for c in result["checks"] if c["name"] == "mean_relevancy")
        assert relevancy_check["passed"] is False

    def test_fails_when_latency_exceeds_ceiling(self):
        result = run_gate(_report(latency_p95_ms=9999.0))
        assert result["passed"] is False
        latency_check = next(c for c in result["checks"] if c["name"] == "latency_p95_ms")
        assert latency_check["passed"] is False

    def test_missing_perf_fields_are_skipped_not_failed(self):
        result = run_gate(_report())  # no latency/error_rate/cost
        skipped = [c for c in result["checks"] if c["skipped"]]
        assert len(skipped) == 3  # latency, error_rate, cost_per_request

    def test_passes_when_all_five_metrics_within_bounds(self):
        result = run_gate(_report(
            mean_relevancy=0.9,
            mean_correctness=0.8,
            latency_p95_ms=500.0,
            error_rate=0.01,
            cost_per_request_usd=0.001,
        ))
        assert result["passed"] is True
        assert all(not c["skipped"] for c in result["checks"])

    def test_fails_when_error_rate_exceeds_ceiling(self):
        result = run_gate(_report(error_rate=0.99))
        assert result["passed"] is False
        err_check = next(c for c in result["checks"] if c["name"] == "error_rate")
        assert err_check["passed"] is False
