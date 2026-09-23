"""Regression tests for MM-02 fix: honest eval status when judge queue publish fails.

Four required cases:
  1. publish success → eval status is "pending"
  2. publish failure (mocked) → eval status is "unavailable"
  3. publish failure is logged at WARNING level
  4. The 15-field AskResponse shape is unchanged (versions field added in 3G)
"""
from __future__ import annotations

import logging
import pytest


# ---------------------------------------------------------------------------
# Case 1 — Publish success → eval status is "pending"
# ---------------------------------------------------------------------------

class TestPublishSuccess:
    def test_eval_status_pending_on_success(self, monkeypatch):
        """When judge_queue.publish() returns True, submit_judge returns
        a dict with status='pending' and a trace_id."""
        import src.judge.async_runner as ar

        monkeypatch.setattr("src.rag.config.JUDGE_QUEUE_ENABLED", True)

        class _FakeQueue:
            def publish(self, trace_id, payload):
                return True  # success

        monkeypatch.setattr(ar, "_published_judges", 0)

        import importlib, sys
        # Patch the redis_queue module reference used inside the function
        import types as _types
        fake_module = _types.SimpleNamespace(judge_queue=_FakeQueue())
        monkeypatch.setitem(sys.modules, "src.judge.redis_queue", fake_module)

        result = ar.submit_judge(
            trace_id="trace-ok",
            question="q",
            answer="a",
            contexts=[],
            evaluate_fn=lambda *a, **kw: {},
        )

        assert result["status"] == "pending", (
            f"publish success must yield status='pending', got {result!r}"
        )
        assert result.get("trace_id") == "trace-ok"


# ---------------------------------------------------------------------------
# Case 2 — Publish failure → eval status is "unavailable"
# ---------------------------------------------------------------------------

class TestPublishFailure:
    def test_eval_status_unavailable_on_publish_failure(self, monkeypatch):
        """When judge_queue.publish() returns False, submit_judge returns
        a dict with status='unavailable'."""
        import src.judge.async_runner as ar
        import sys, types as _types

        monkeypatch.setattr("src.rag.config.JUDGE_QUEUE_ENABLED", True)

        class _FailQueue:
            def publish(self, trace_id, payload):
                return False  # Redis down / queue full

        fake_module = _types.SimpleNamespace(judge_queue=_FailQueue())
        monkeypatch.setitem(sys.modules, "src.judge.redis_queue", fake_module)

        result = ar.submit_judge(
            trace_id="trace-fail",
            question="q",
            answer="a",
            contexts=[],
            evaluate_fn=lambda *a, **kw: {},
        )

        assert result["status"] == "unavailable", (
            f"publish failure must yield status='unavailable', got {result!r}"
        )
        assert result.get("reason") == "queue_unavailable"

    def test_publish_failure_does_not_raise(self, monkeypatch):
        """submit_judge must not raise when publish() returns False."""
        import src.judge.async_runner as ar
        import sys, types as _types

        monkeypatch.setattr("src.rag.config.JUDGE_QUEUE_ENABLED", True)

        class _FailQueue:
            def publish(self, trace_id, payload):
                return False

        fake_module = _types.SimpleNamespace(judge_queue=_FailQueue())
        monkeypatch.setitem(sys.modules, "src.judge.redis_queue", fake_module)

        try:
            ar.submit_judge(
                trace_id="trace-no-raise",
                question="q",
                answer="a",
                contexts=[],
                evaluate_fn=lambda *a, **kw: {},
            )
        except Exception as exc:
            pytest.fail(f"submit_judge raised unexpectedly on publish failure: {exc!r}")


# ---------------------------------------------------------------------------
# Case 3 — Publish failure is logged at WARNING level
# ---------------------------------------------------------------------------

class TestPublishFailureIsLogged:
    def test_warning_logged_on_publish_failure(self, monkeypatch, caplog):
        """When publish() returns False, a WARNING-level message is emitted."""
        import src.judge.async_runner as ar
        import sys, types as _types

        monkeypatch.setattr("src.rag.config.JUDGE_QUEUE_ENABLED", True)

        class _FailQueue:
            def publish(self, trace_id, payload):
                return False

        fake_module = _types.SimpleNamespace(judge_queue=_FailQueue())
        monkeypatch.setitem(sys.modules, "src.judge.redis_queue", fake_module)

        with caplog.at_level(logging.WARNING, logger="src.judge.async_runner"):
            ar.submit_judge(
                trace_id="trace-log",
                question="q",
                answer="a",
                contexts=[],
                evaluate_fn=lambda *a, **kw: {},
            )

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("trace-log" in msg or "publish failed" in msg for msg in warning_messages), (
            f"Expected a WARNING log mentioning trace_id or 'publish failed', got: {warning_messages!r}"
        )


# ---------------------------------------------------------------------------
# Case 4 — The 15-field AskResponse shape is unchanged (versions added in 3G)
# ---------------------------------------------------------------------------

class TestAskResponseShapeUnchanged:
    def test_ask_response_has_15_fields(self):
        """AskResponse must have exactly 15 fields after the 3G versions field was added."""
        from src.api.app import AskResponse

        fields = AskResponse.model_fields
        assert len(fields) == 15, (
            f"AskResponse must have 15 fields, found {len(fields)}: {list(fields.keys())}"
        )

    def test_eval_field_exists_and_accepts_unavailable_status(self):
        """The eval field must still exist and accept the new unavailable dict."""
        from src.api.app import AskResponse

        resp = AskResponse(
            answer="test answer",
            route="vector",
            strategy="adaptive",
            model="deepseek/deepseek-flash",
            sources=[],
            latency_ms=100.0,
            tokens={"prompt": 10, "completion": 5, "total": 15},
            estimated_cost_usd=0.0001,
            tenant="test",
            eval={"status": "unavailable", "reason": "queue_unavailable"},
            guard=None,
            trace=[],
            thread_id=None,
            transcript=None,
        )

        assert resp.eval == {"status": "unavailable", "reason": "queue_unavailable"}, (
            "eval field must accept the unavailable status dict"
        )
