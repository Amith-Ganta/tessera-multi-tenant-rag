"""Tests for judge feedback injection into analytics (Item 5, Hardening 2026-09-24).

Verifies that _emit_judge_quality_signal() writes a 'judge_quality_fail' event
to analytics whenever a tracked metric (faithfulness, answer_relevancy,
correctness) fails its threshold, and that passing runs do NOT write events.
"""
from __future__ import annotations

import importlib
from unittest.mock import patch, MagicMock


def _reload_module():
    import src.judge.async_runner as ar
    importlib.reload(ar)
    return ar


# ---------------------------------------------------------------------------
# Test 1: failed faithfulness triggers analytics write
# ---------------------------------------------------------------------------

def test_failed_faithfulness_emits_quality_signal():
    logged = []

    result = {
        "status": "done",
        "metrics": {
            "faithfulness": {"score": 0.3, "passed": False, "threshold": 0.7, "reason": "low"},
            "answer_relevancy": {"score": 0.9, "passed": True, "threshold": 0.7},
        },
    }

    with patch("src.rag.analytics.log_analytics", side_effect=logged.append):
        import src.judge.async_runner as ar
        ar._emit_judge_quality_signal("trace-001", result)

    assert len(logged) == 1, "Expected one analytics record"
    record = logged[0]
    assert record["event"] == "judge_quality_fail"
    assert record["trace_id"] == "trace-001"
    assert "faithfulness" in record["failed_metrics"]
    assert "answer_relevancy" not in record["failed_metrics"], "passing metric must not appear"


# ---------------------------------------------------------------------------
# Test 2: all metrics passing — no analytics write
# ---------------------------------------------------------------------------

def test_all_passing_emits_no_signal():
    logged = []

    result = {
        "status": "done",
        "metrics": {
            "faithfulness": {"score": 0.85, "passed": True, "threshold": 0.7},
            "answer_relevancy": {"score": 0.9, "passed": True, "threshold": 0.7},
            "correctness": {"score": 0.8, "passed": True, "threshold": 0.7},
        },
    }

    with patch("src.rag.analytics.log_analytics", side_effect=logged.append):
        import src.judge.async_runner as ar
        ar._emit_judge_quality_signal("trace-002", result)

    assert len(logged) == 0, "No analytics record expected when all metrics pass"


# ---------------------------------------------------------------------------
# Test 3: no metrics key — no crash, no write
# ---------------------------------------------------------------------------

def test_no_metrics_no_crash():
    logged = []

    with patch("src.rag.analytics.log_analytics", side_effect=logged.append):
        import src.judge.async_runner as ar
        ar._emit_judge_quality_signal("trace-003", {"status": "done"})

    assert len(logged) == 0


# ---------------------------------------------------------------------------
# Test 4: non-tracked metric failure does not trigger write
# ---------------------------------------------------------------------------

def test_non_tracked_metric_failure_ignored():
    """Toxicity failures are not in _QUALITY_SIGNAL_METRICS and must not emit."""
    logged = []

    result = {
        "status": "done",
        "metrics": {
            "toxicity": {"score": 0.6, "passed": False, "threshold": 0.5},
            "faithfulness": {"score": 0.9, "passed": True, "threshold": 0.7},
        },
    }

    with patch("src.rag.analytics.log_analytics", side_effect=logged.append):
        import src.judge.async_runner as ar
        ar._emit_judge_quality_signal("trace-004", result)

    assert len(logged) == 0


# ---------------------------------------------------------------------------
# Test 5: analytics failure does not propagate
# ---------------------------------------------------------------------------

def test_analytics_failure_does_not_raise():
    """_emit_judge_quality_signal must never raise, even if log_analytics blows up."""
    result = {
        "status": "done",
        "metrics": {
            "faithfulness": {"score": 0.2, "passed": False, "threshold": 0.7},
        },
    }

    with patch("src.rag.analytics.log_analytics", side_effect=RuntimeError("disk full")):
        import src.judge.async_runner as ar
        # Must not raise
        ar._emit_judge_quality_signal("trace-005", result)
