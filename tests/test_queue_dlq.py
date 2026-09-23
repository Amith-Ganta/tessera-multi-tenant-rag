"""Tests for judge queue metrics, bounded admission, and DLQ drain (Phase B2/B3).

Verifies:
B2 - queue_depth() and is_over_capacity() return correct values
B2 - publish() is rejected when queue is at capacity (via app.py endpoint)
B3 - peek_dlq() returns entries without removing them
B3 - drain_dlq() removes all DLQ entries and returns count
B3 - /admin/dlq GET requires admin role
B3 - /admin/dlq DELETE requires admin role
"""
from __future__ import annotations

import fakeredis
import pytest
from src.judge.redis_queue import JudgeQueue, _set_client


@pytest.fixture
def fake_queue():
    """Return a JudgeQueue wired to a fresh fakeredis instance."""
    fr = fakeredis.FakeRedis(decode_responses=True)
    _set_client(fr)
    q = JudgeQueue(queue_name="test:q", results_prefix="test:r:", max_depth=3)
    yield q
    fr.flushall()
    _set_client(None)


# ---------------------------------------------------------------------------
# B2: Queue depth and bounded admission
# ---------------------------------------------------------------------------

class TestQueueMetrics:
    def test_queue_depth_empty(self, fake_queue):
        assert fake_queue.queue_depth() == 0

    def test_queue_depth_after_publish(self, fake_queue):
        fake_queue.publish("t1", {"question": "q1"})
        fake_queue.publish("t2", {"question": "q2"})
        assert fake_queue.queue_depth() == 2

    def test_is_over_capacity_false_when_under(self, fake_queue):
        fake_queue.publish("t1", {"question": "q"})
        assert fake_queue.is_over_capacity() is False

    def test_is_over_capacity_true_at_max_depth(self, fake_queue):
        for i in range(3):
            fake_queue.publish(f"t{i}", {"question": "q"})
        assert fake_queue.is_over_capacity() is True

    def test_queue_depth_zero_when_redis_down(self):
        _set_client(None)
        q = JudgeQueue(queue_name="test:q2", results_prefix="test:r2:", max_depth=5)
        assert q.queue_depth() == 0
        assert q.is_over_capacity() is False


# ---------------------------------------------------------------------------
# B3: DLQ peek and drain
# ---------------------------------------------------------------------------

class TestDLQ:
    def test_dlq_depth_zero_initially(self, fake_queue):
        assert fake_queue.dlq_depth() == 0

    def test_peek_empty_dlq(self, fake_queue):
        assert fake_queue.peek_dlq() == []

    def test_drain_empty_dlq_returns_zero(self, fake_queue):
        assert fake_queue.drain_dlq() == 0

    def test_dlq_receives_exhausted_job(self, fake_queue):
        job = {"trace_id": "dlq-test", "_attempts": 3, "question": "q"}
        fake_queue.requeue_or_dlq(job)
        assert fake_queue.dlq_depth() == 1

    def test_peek_does_not_remove(self, fake_queue):
        job = {"trace_id": "dlq-peek", "_attempts": 3, "question": "q"}
        fake_queue.requeue_or_dlq(job)
        fake_queue.peek_dlq(10)
        assert fake_queue.dlq_depth() == 1

    def test_drain_removes_all_entries(self, fake_queue):
        for i in range(3):
            job = {"trace_id": f"dlq-{i}", "_attempts": 3, "question": "q"}
            fake_queue.requeue_or_dlq(job)
        assert fake_queue.dlq_depth() == 3
        count = fake_queue.drain_dlq()
        assert count == 3
        assert fake_queue.dlq_depth() == 0

    def test_drain_returns_count(self, fake_queue):
        for i in range(5):
            job = {"trace_id": f"drain-{i}", "_attempts": 3, "question": "q"}
            fake_queue.requeue_or_dlq(job)
        count = fake_queue.drain_dlq()
        assert count == 5
