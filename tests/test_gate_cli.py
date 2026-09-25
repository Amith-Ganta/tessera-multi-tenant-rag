"""Item 14: Fail-closed quality gate — CLI and integration tests.

Covers:
- main() exits 0 when all metrics pass
- main() exits 1 when any metric fails
- main() exits 1 when the report file is missing
- main() prints PASS/FAIL/SKIP lines to stdout
- run_gate() is fail-closed: corrupt/missing data never silently passes
- run_gate() treats an all-skipped report as passed (intentional design —
  skip ≠ fail; an absent metric is not a regression)
- run_gate() with a real JSON file via _load_report() round-trip

No real LLM calls are made.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import pytest

from evals.gate import run_gate, main as gate_main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _passing_report() -> dict:
    return {
        "aggregates": {
            "mean_relevancy": 0.9,
            "mean_relevancy_vector": 0.9,
            "mean_correctness": 0.8,
            "mean_correctness_vector": 0.8,
        },
        "performance": {
            "latency_p95_ms": 500.0,
            "error_rate": 0.01,
            "cost_per_request_usd": 0.001,
        },
    }


def _failing_report() -> dict:
    r = _passing_report()
    r["aggregates"]["mean_relevancy"] = 0.1        # below MIN_MEAN_RELEVANCY
    r["aggregates"]["mean_relevancy_vector"] = 0.1  # gate checks this key
    return r


# ---------------------------------------------------------------------------
# CLI: exit codes
# ---------------------------------------------------------------------------

class TestGateCliExitCodes:
    def test_exits_0_when_all_pass(self, tmp_path):
        report_file = tmp_path / "latest.json"
        report_file.write_text(json.dumps(_passing_report()), encoding="utf-8")
        with patch("evals.gate.LATEST_REPORT_PATH", report_file):
            code = gate_main()
        assert code == 0

    def test_exits_1_when_metric_fails(self, tmp_path):
        report_file = tmp_path / "latest.json"
        report_file.write_text(json.dumps(_failing_report()), encoding="utf-8")
        with patch("evals.gate.LATEST_REPORT_PATH", report_file):
            code = gate_main()
        assert code == 1

    def test_exits_1_when_report_missing(self, tmp_path):
        missing = tmp_path / "no_such_file.json"
        with patch("evals.gate.LATEST_REPORT_PATH", missing):
            code = gate_main()
        assert code == 1


# ---------------------------------------------------------------------------
# CLI: stdout output format
# ---------------------------------------------------------------------------

class TestGateCliOutput:
    def _run_with_capture(self, report: dict, tmp_path: Path) -> str:
        report_file = tmp_path / "latest.json"
        report_file.write_text(json.dumps(report), encoding="utf-8")
        buf = StringIO()
        with patch("evals.gate.LATEST_REPORT_PATH", report_file), \
             patch("sys.stdout", buf):
            gate_main()
        return buf.getvalue()

    def test_pass_line_printed_for_passing_check(self, tmp_path):
        out = self._run_with_capture(_passing_report(), tmp_path)
        assert "PASS" in out

    def test_fail_line_printed_for_failing_check(self, tmp_path):
        out = self._run_with_capture(_failing_report(), tmp_path)
        assert "FAIL" in out

    def test_skip_line_printed_for_absent_metric(self, tmp_path):
        # Report with only quality metrics; no performance fields
        report = {"aggregates": {"mean_relevancy": 0.9, "mean_correctness": 0.8}}
        out = self._run_with_capture(report, tmp_path)
        assert "SKIP" in out

    def test_gate_passed_message_on_success(self, tmp_path):
        out = self._run_with_capture(_passing_report(), tmp_path)
        assert "GATE PASSED" in out

    def test_gate_failed_message_on_failure(self, tmp_path):
        out = self._run_with_capture(_failing_report(), tmp_path)
        assert "GATE FAILED" in out


# ---------------------------------------------------------------------------
# Fail-closed: run_gate() guarantees
# ---------------------------------------------------------------------------

class TestGateFailClosed:
    def test_totally_empty_report_still_passes(self):
        """Empty report: all checks skipped → passed=True.

        This is intentional design: absence of a metric is NOT a regression.
        A fresh environment with no eval data should not block deployment.
        """
        r = run_gate({})
        assert r["passed"] is True
        assert all(c["skipped"] for c in r["checks"])

    def test_all_metrics_present_and_bad_fails_gate(self):
        report = {
            "aggregates": {
                "mean_relevancy": 0.0,
                "mean_correctness": 0.0,
                "mean_correctness_vector": 0.0,
            },
            "performance": {
                "latency_p95_ms": 999999.0,
                "error_rate": 1.0,
                "cost_per_request_usd": 999.0,
            },
        }
        r = run_gate(report)
        assert r["passed"] is False
        assert all(not c["passed"] for c in r["checks"] if not c["skipped"])

    def test_corrupt_value_skipped_not_passed(self):
        """A non-numeric value is skipped (not treated as passing)."""
        r = run_gate({"aggregates": {"mean_relevancy_vector": None}})
        rel = next(c for c in r["checks"] if c["name"] == "mean_relevancy_vector")
        assert rel["skipped"] is True
        assert rel["passed"] is True  # skipped defaults to passed=True per design

    def test_corrupt_value_does_not_allow_bad_latency_to_pass(self):
        """Corrupt relevancy is skipped; a real latency failure still fails the gate."""
        r = run_gate({
            "aggregates": {"mean_relevancy": "bad"},
            "performance": {"latency_p95_ms": 99999.0},
        })
        assert r["passed"] is False

    def test_gate_returns_all_five_checks_regardless_of_input(self):
        for report in [{}, {"aggregates": {}}, _passing_report()]:
            r = run_gate(report)
            assert len(r["checks"]) == 5, (
                f"Gate must always return 5 checks; got {len(r['checks'])} for {report}"
            )

    def test_gate_passed_is_boolean(self):
        r = run_gate(_passing_report())
        assert isinstance(r["passed"], bool)
        r2 = run_gate(_failing_report())
        assert isinstance(r2["passed"], bool)

    def test_gate_uses_vector_correctness_not_mixed(self):
        """Gate must pass when vector-route correctness ≥ threshold even if
        mixed mean_correctness is low (direct-route items drag it down)."""
        report = {
            "aggregates": {
                "mean_relevancy": 0.9,
                "mean_correctness": 0.3,        # mixed — below threshold
                "mean_correctness_vector": 0.75,  # vector-only — above threshold
            },
        }
        r = run_gate(report)
        correctness_check = next(
            c for c in r["checks"] if c["name"] == "mean_correctness_vector"
        )
        assert correctness_check["passed"] is True, (
            "Gate must check mean_correctness_vector, not mean_correctness"
        )

    def test_gate_fails_when_vector_correctness_below_threshold(self):
        """Gate must fail when vector-route correctness falls below 0.5."""
        report = {
            "aggregates": {
                "mean_relevancy": 0.9,
                "mean_correctness": 0.8,
                "mean_correctness_vector": 0.3,  # vector-route below threshold
            },
        }
        r = run_gate(report)
        assert r["passed"] is False
        correctness_check = next(
            c for c in r["checks"] if c["name"] == "mean_correctness_vector"
        )
        assert correctness_check["passed"] is False


# ---------------------------------------------------------------------------
# Round-trip: _load_report() reads what we write
# ---------------------------------------------------------------------------

class TestLoadReportRoundTrip:
    def test_round_trip_valid_json(self, tmp_path):
        from evals.gate import _load_report
        report_file = tmp_path / "latest.json"
        report_file.write_text(json.dumps(_passing_report()), encoding="utf-8")
        with patch("evals.gate.LATEST_REPORT_PATH", report_file):
            loaded = _load_report()
        assert loaded is not None
        assert loaded["aggregates"]["mean_relevancy"] == pytest.approx(0.9)

    def test_load_report_returns_none_when_missing(self, tmp_path):
        from evals.gate import _load_report
        missing = tmp_path / "no_such.json"
        with patch("evals.gate.LATEST_REPORT_PATH", missing):
            result = _load_report()
        assert result is None

    def test_load_report_raises_on_non_object_json(self, tmp_path):
        from evals.gate import _load_report
        report_file = tmp_path / "latest.json"
        report_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with patch("evals.gate.LATEST_REPORT_PATH", report_file):
            with pytest.raises(ValueError, match="JSON object"):
                _load_report()
