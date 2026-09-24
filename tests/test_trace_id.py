"""Item 20: End-to-end trace ID tests.

Covers:
- submit_judge() returns a dict with "status" and "trace_id" keys
- trace_id is a non-empty string in the submit_judge response
- trace_id in response is the same one passed in (not regenerated)
- judge_store records a pending entry keyed by the trace_id
- _emit_judge_quality_signal() writes trace_id to analytics log
- analytics log entry for judge_quality_fail includes trace_id field
- Two different submit_judge calls produce independent trace IDs in store
- A trace_id can be looked up in judge_store after submit

No real Redis, LLM, or filesystem calls are made.
"""

from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import MagicMock, patch

# Stub langchain_chroma before any src.rag imports.
_stub_chroma = types.ModuleType("langchain_chroma")
_stub_chroma.Chroma = MagicMock()
sys.modules.setdefault("langchain_chroma", _stub_chroma)


class TestSubmitJudgeTraceIdContract:
    """submit_judge() must return a dict carrying the trace_id."""

    def _call_submit(self, trace_id: str | None = None, *, queue_enabled: bool = False) -> dict:
        from src.judge.judge_store import judge_store
        from src.judge.async_runner import submit_judge

        if trace_id is None:
            trace_id = str(uuid.uuid4())

        evaluate_fn = lambda q, a, c: {"status": "done", "metrics": {}}

        with patch("src.judge.async_runner.JUDGE_QUEUE_ENABLED", queue_enabled, create=True), \
             patch("src.rag.config.JUDGE_QUEUE_ENABLED", queue_enabled, create=True):
            if queue_enabled:
                mock_queue = MagicMock()
                mock_queue.publish.return_value = True
                with patch("src.judge.async_runner.judge_queue", mock_queue, create=True):
                    result = submit_judge(
                        trace_id=trace_id,
                        question="test question",
                        answer="test answer",
                        contexts=["ctx"],
                        evaluate_fn=evaluate_fn,
                    )
            else:
                with patch("asyncio.create_task"):
                    result = submit_judge(
                        trace_id=trace_id,
                        question="test question",
                        answer="test answer",
                        contexts=["ctx"],
                        evaluate_fn=evaluate_fn,
                    )
        return result, trace_id

    def test_submit_judge_returns_dict(self):
        result, _ = self._call_submit()
        assert isinstance(result, dict)

    def test_submit_judge_returns_status_field(self):
        result, _ = self._call_submit()
        assert "status" in result

    def test_submit_judge_returns_trace_id_field(self):
        result, _ = self._call_submit()
        assert "trace_id" in result

    def test_submit_judge_trace_id_matches_input(self):
        tid = str(uuid.uuid4())
        result, _ = self._call_submit(trace_id=tid)
        assert result["trace_id"] == tid

    def test_submit_judge_trace_id_non_empty(self):
        result, _ = self._call_submit()
        assert isinstance(result["trace_id"], str) and len(result["trace_id"]) > 0

    def test_submit_judge_status_is_pending(self):
        result, _ = self._call_submit()
        assert result["status"] == "pending"

    def test_submit_judge_queue_mode_returns_trace_id(self):
        """Queue mode: patch the redis_queue module so publish() succeeds."""
        import src.judge.redis_queue as rq_mod
        from src.judge.async_runner import submit_judge
        from src.judge.judge_store import judge_store

        tid = str(uuid.uuid4())
        mock_queue = MagicMock()
        mock_queue.publish.return_value = True

        with patch.object(rq_mod, "judge_queue", mock_queue), \
             patch("src.rag.config.JUDGE_QUEUE_ENABLED", True, create=True), \
             patch("src.judge.async_runner.JUDGE_QUEUE_ENABLED", True, create=True):
            result = submit_judge(
                trace_id=tid,
                question="q",
                answer="a",
                contexts=["ctx"],
                evaluate_fn=lambda q, a, c: {},
            )

        assert result.get("trace_id") == tid
        assert result.get("status") == "pending"


class TestJudgeStoreTraceIdPersistence:
    """judge_store must record trace_id so GET /eval/{trace_id} can find it."""

    def _submit(self, trace_id: str) -> None:
        from src.judge.async_runner import submit_judge
        with patch("src.rag.config.JUDGE_QUEUE_ENABLED", False, create=True):
            with patch("asyncio.create_task"):
                submit_judge(
                    trace_id=trace_id,
                    question="q",
                    answer="a",
                    contexts=[],
                    evaluate_fn=lambda q, a, c: {},
                )

    def test_trace_id_stored_as_pending_in_judge_store(self):
        from src.judge.judge_store import judge_store
        tid = str(uuid.uuid4())
        self._submit(tid)
        entry = judge_store.get(tid)
        assert entry is not None

    def test_stored_entry_has_pending_status(self):
        from src.judge.judge_store import judge_store
        tid = str(uuid.uuid4())
        self._submit(tid)
        entry = judge_store.get(tid)
        assert entry.get("status") == "pending"

    def test_two_different_trace_ids_are_independent(self):
        from src.judge.judge_store import judge_store
        tid1 = str(uuid.uuid4())
        tid2 = str(uuid.uuid4())
        self._submit(tid1)
        self._submit(tid2)
        assert judge_store.get(tid1) is not None
        assert judge_store.get(tid2) is not None
        assert tid1 != tid2

    def test_unknown_trace_id_returns_none(self):
        from src.judge.judge_store import judge_store
        entry = judge_store.get("completely-unknown-id-that-does-not-exist-xyz")
        assert entry is None


class TestEmitJudgeQualitySignalTraceId:
    """_emit_judge_quality_signal() must include trace_id in the analytics record."""

    def test_quality_fail_emits_trace_id_in_analytics(self):
        from src.judge.async_runner import _emit_judge_quality_signal

        captured = []

        def mock_log(record):
            captured.append(record)

        with patch("src.rag.analytics.log_analytics", mock_log):
            _emit_judge_quality_signal(
                trace_id="test-trace-abc-123",
                result={
                    "metrics": {
                        "faithfulness": {
                            "score": 0.2,
                            "threshold": 0.5,
                            "passed": False,
                            "reason": "low score",
                        }
                    }
                },
            )

        assert len(captured) == 1
        assert captured[0]["trace_id"] == "test-trace-abc-123"

    def test_quality_fail_analytics_event_name(self):
        from src.judge.async_runner import _emit_judge_quality_signal

        captured = []
        with patch("src.rag.analytics.log_analytics", captured.append):
            _emit_judge_quality_signal(
                trace_id="trace-x",
                result={
                    "metrics": {
                        "answer_relevancy": {
                            "score": 0.1,
                            "threshold": 0.6,
                            "passed": False,
                            "reason": "off-topic",
                        }
                    }
                },
            )

        assert captured[0]["event"] == "judge_quality_fail"

    def test_quality_pass_does_not_emit_analytics(self):
        from src.judge.async_runner import _emit_judge_quality_signal

        captured = []
        with patch("src.rag.analytics.log_analytics", captured.append):
            _emit_judge_quality_signal(
                trace_id="trace-pass",
                result={
                    "metrics": {
                        "faithfulness": {
                            "score": 0.9,
                            "threshold": 0.5,
                            "passed": True,
                        }
                    }
                },
            )

        # No failures → no analytics event emitted
        assert len(captured) == 0

    def test_empty_metrics_does_not_emit_analytics(self):
        from src.judge.async_runner import _emit_judge_quality_signal

        captured = []
        with patch("src.rag.analytics.log_analytics", captured.append):
            _emit_judge_quality_signal(trace_id="trace-empty", result={"metrics": {}})

        assert len(captured) == 0
