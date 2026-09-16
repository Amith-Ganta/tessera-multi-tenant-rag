"""Tests for Phase 4a: async judging — JudgeStore and submit_judge."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.judge.judge_store import JudgeStore


# ---------------------------------------------------------------------------
# JudgeStore unit tests (7 cases)
# ---------------------------------------------------------------------------

class TestJudgeStore:
    def test_set_pending_marks_pending(self):
        store = JudgeStore(max_size=10)
        store.set_pending("t1")
        assert store.get("t1") == {"status": "pending"}

    def test_set_result_overwrites_pending(self):
        store = JudgeStore(max_size=10)
        store.set_pending("t2")
        store.set_result("t2", {"status": "done", "enabled": True, "metrics": {}})
        result = store.get("t2")
        assert result is not None
        assert result["status"] == "done"

    def test_get_unknown_returns_none(self):
        store = JudgeStore(max_size=10)
        assert store.get("nonexistent") is None

    def test_ring_buffer_evicts_oldest(self):
        store = JudgeStore(max_size=3)
        store.set_pending("a")
        store.set_pending("b")
        store.set_pending("c")
        # Adding a 4th evicts "a"
        store.set_pending("d")
        assert store.get("a") is None
        assert store.get("b") is not None
        assert store.get("c") is not None
        assert store.get("d") is not None

    def test_ring_buffer_respects_max_size(self):
        store = JudgeStore(max_size=5)
        for i in range(10):
            store.set_pending(str(i))
        # Only the last 5 should remain
        for i in range(5):
            assert store.get(str(i)) is None
        for i in range(5, 10):
            assert store.get(str(i)) is not None

    def test_thread_safety_concurrent_writes(self):
        store = JudgeStore(max_size=1000)
        errors: list[Exception] = []

        def writer(tid: str) -> None:
            try:
                store.set_pending(tid)
                store.set_result(tid, {"status": "done"})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(str(i),)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread-safety errors: {errors}"

    def test_set_result_for_unknown_trace_id_stores_it(self):
        """set_result should create an entry even if set_pending was never called."""
        store = JudgeStore(max_size=10)
        store.set_result("orphan", {"status": "done", "metrics": {}})
        result = store.get("orphan")
        assert result is not None
        assert result["status"] == "done"


# ---------------------------------------------------------------------------
# submit_judge integration test (requires asyncio event loop)
# ---------------------------------------------------------------------------

class TestSubmitJudge:
    def test_submit_judge_sets_pending_then_done(self):
        """submit_judge() registers pending immediately and resolves to done."""
        from src.judge.async_runner import submit_judge
        from src.judge.judge_store import judge_store

        call_log: list[str] = []

        def fake_evaluate(question: str, answer: str, contexts: list[str]) -> dict:
            call_log.append(question)
            return {"enabled": True, "metrics": {"fake": {"score": 1.0, "passed": True}}}

        trace_id = "test-async-trace-001"

        async def run_test() -> None:
            submit_judge(
                trace_id=trace_id,
                question="test question",
                answer="test answer",
                contexts=["ctx1"],
                evaluate_fn=fake_evaluate,
            )
            # Immediately after scheduling, status must be pending
            assert judge_store.get(trace_id) == {"status": "pending"}
            # Allow the background task to complete
            await asyncio.sleep(0.5)
            result = judge_store.get(trace_id)
            assert result is not None
            assert result["status"] == "done"
            assert call_log == ["test question"]

        asyncio.run(run_test())

    def test_submit_judge_stores_error_on_exception(self):
        """When evaluate_fn raises, judge_store gets status=error."""
        from src.judge.async_runner import submit_judge
        from src.judge.judge_store import judge_store

        def failing_evaluate(question: str, answer: str, contexts: list[str]) -> dict:
            raise RuntimeError("simulated judge failure")

        trace_id = "test-async-trace-002"

        async def run_test() -> None:
            submit_judge(
                trace_id=trace_id,
                question="q",
                answer="a",
                contexts=[],
                evaluate_fn=failing_evaluate,
            )
            await asyncio.sleep(0.5)
            result = judge_store.get(trace_id)
            assert result is not None
            assert result["status"] == "error"
            assert "reason" in result

        asyncio.run(run_test())
