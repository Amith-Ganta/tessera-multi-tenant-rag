"""Tests for Phase 5 Part 2: Redis judge queue (fakeredis-based).

All 8 cases use fakeredis so no live Redis is needed.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from src.judge.redis_queue import JudgeQueue, _set_client, MAX_JOB_ATTEMPTS


@pytest.fixture(autouse=True)
def fake_redis_client():
    """Inject a fakeredis client and reset it between tests."""
    client = fakeredis.FakeRedis(decode_responses=True)
    _set_client(client)
    yield client
    _set_client(None)


def make_queue(**kwargs) -> JudgeQueue:
    return JudgeQueue(
        queue_name=kwargs.get("queue_name", "test:queue"),
        results_prefix=kwargs.get("results_prefix", "test:result:"),
        max_depth=kwargs.get("max_depth", 10),
    )


class TestPublish:
    def test_publish_returns_true_and_pushes(self, fake_redis_client):
        q = make_queue()
        ok = q.publish("trace-1", {"question": "q", "answer": "a", "contexts": []})
        assert ok is True
        assert fake_redis_client.llen("test:queue") == 1

    def test_published_payload_contains_trace_id(self, fake_redis_client):
        q = make_queue()
        q.publish("trace-2", {"question": "hello", "answer": "world", "contexts": ["c1"]})
        raw = fake_redis_client.brpop("test:queue", timeout=1)
        assert raw is not None
        data = json.loads(raw[1])
        assert data["trace_id"] == "trace-2"
        assert data["question"] == "hello"

    def test_multiple_publishes_stack(self, fake_redis_client):
        q = make_queue()
        for i in range(5):
            q.publish(f"t{i}", {"question": str(i), "answer": "", "contexts": []})
        assert q.queue_depth() == 5


class TestResultStore:
    def test_set_and_get_result(self, fake_redis_client):
        q = make_queue()
        q.set_result("tid-1", {"status": "done", "score": 0.9})
        result = q.get_result("tid-1")
        assert result is not None
        assert result["status"] == "done"
        assert result["score"] == pytest.approx(0.9)

    def test_get_result_missing_returns_none(self, fake_redis_client):
        q = make_queue()
        assert q.get_result("nonexistent") is None

    def test_result_ttl_is_set(self, fake_redis_client):
        q = make_queue()
        q.set_result("tid-ttl", {"status": "done"}, ttl=3600)
        ttl = fake_redis_client.ttl("test:result:tid-ttl")
        assert ttl > 0


class TestCapacity:
    def test_is_over_capacity_when_full(self, fake_redis_client):
        q = make_queue(max_depth=3)
        for i in range(3):
            q.publish(f"cap-{i}", {"question": "", "answer": "", "contexts": []})
        assert q.is_over_capacity() is True

    def test_not_over_capacity_when_below_limit(self, fake_redis_client):
        q = make_queue(max_depth=10)
        q.publish("one", {"question": "", "answer": "", "contexts": []})
        assert q.is_over_capacity() is False


class TestBlockingPop:
    def test_blocking_pop_returns_job(self, fake_redis_client):
        q = make_queue()
        q.publish("pop-1", {"question": "q?", "answer": "a!", "contexts": ["x"]})
        job = q.blocking_pop(timeout=1)
        assert job is not None
        assert job["trace_id"] == "pop-1"
        assert job["question"] == "q?"

    def test_blocking_pop_empty_queue_returns_none(self, fake_redis_client):
        q = make_queue()
        # timeout=1 to avoid BRPOP timeout=0 (which blocks forever in Redis)
        job = q.blocking_pop(timeout=1)
        assert job is None

    def test_blocking_pop_increments_attempts(self, fake_redis_client):
        q = make_queue()
        q.publish("attempts-1", {"question": "q", "answer": "a", "contexts": []})
        job = q.blocking_pop(timeout=1)
        assert job is not None
        assert job["_attempts"] == 1

    def test_blocking_pop_increments_attempts_on_retry(self, fake_redis_client):
        q = make_queue()
        # Simulate a job that was already attempted once and re-queued
        import json
        job_data = json.dumps({"trace_id": "retry-1", "question": "q", "_attempts": 1})
        fake_redis_client.lpush("test:queue", job_data)
        job = q.blocking_pop(timeout=1)
        assert job is not None
        assert job["_attempts"] == 2


class TestDLQ:
    """Regression tests for DLQ and retry counter."""

    def test_requeue_under_limit_goes_back_to_main_queue(self, fake_redis_client):
        q = make_queue()
        q.publish("dlq-1", {"question": "q", "answer": "a", "contexts": []})
        job = q.blocking_pop(timeout=1)  # _attempts == 1
        assert job["_attempts"] == 1
        q.requeue_or_dlq(job)
        # Should be back in the main queue
        assert fake_redis_client.llen("test:queue") == 1
        assert fake_redis_client.llen("test:queue:dlq") == 0

    def test_requeue_at_max_attempts_goes_to_dlq(self, fake_redis_client):
        q = make_queue()
        import json
        # Job has already hit MAX_JOB_ATTEMPTS - 1 attempts, blocking_pop will make it MAX
        exhausted_data = json.dumps({
            "trace_id": "dlq-exhaust",
            "question": "q",
            "_attempts": MAX_JOB_ATTEMPTS - 1,
        })
        fake_redis_client.lpush("test:queue", exhausted_data)
        job = q.blocking_pop(timeout=1)
        assert job["_attempts"] == MAX_JOB_ATTEMPTS
        q.requeue_or_dlq(job)
        assert fake_redis_client.llen("test:queue:dlq") == 1
        assert fake_redis_client.llen("test:queue") == 0

    def test_dlq_depth_reflects_items(self, fake_redis_client):
        q = make_queue()
        assert q.dlq_depth() == 0
        import json
        for i in range(3):
            fake_redis_client.lpush("test:queue:dlq", json.dumps({"_raw": str(i)}))
        assert q.dlq_depth() == 3


# ---------------------------------------------------------------------------
# GAP-09: Tenant-scoped result keys
# ---------------------------------------------------------------------------

class TestTenantScopedResults:
    """GAP-09 — judge results are keyed by <prefix><tenant>:<trace_id>."""

    def test_set_result_with_tenant_uses_scoped_key(self, fake_redis_client):
        q = make_queue()
        q.set_result("t1", {"status": "done"}, tenant="user-1")
        # Scoped key must exist.
        assert fake_redis_client.exists("test:result:user-1:t1")
        # Legacy key must NOT exist when tenant is given.
        assert not fake_redis_client.exists("test:result:t1")

    def test_get_result_with_tenant_reads_scoped_key(self, fake_redis_client):
        q = make_queue()
        q.set_result("t2", {"score": 0.8}, tenant="user-2")
        result = q.get_result("t2", tenant="user-2")
        assert result is not None
        assert result["score"] == pytest.approx(0.8)

    def test_tenant_a_cannot_read_tenant_b_result(self, fake_redis_client):
        q = make_queue()
        q.set_result("t3", {"secret": True}, tenant="user-3")
        # user-4 key does not exist; legacy fallback finds nothing either.
        result = q.get_result("t3", tenant="user-4")
        assert result is None

    def test_legacy_fallback_reads_unscoped_key(self, fake_redis_client):
        """Results written before GAP-09 (no tenant prefix) must still be readable."""
        import json
        # Simulate old worker: write directly to legacy key.
        fake_redis_client.set("test:result:legacy-trace", json.dumps({"status": "legacy"}))
        q = make_queue()
        # With tenant supplied, fallback to legacy key when scoped key is missing.
        result = q.get_result("legacy-trace", tenant="user-5")
        assert result is not None
        assert result["status"] == "legacy"

    def test_invalidate_by_tenant_deletes_only_that_tenant(self, fake_redis_client):
        q = make_queue()
        q.set_result("r1", {"x": 1}, tenant="user-10")
        q.set_result("r2", {"x": 2}, tenant="user-10")
        q.set_result("r3", {"x": 3}, tenant="user-20")
        deleted = q.invalidate_by_tenant("user-10")
        assert deleted == 2
        assert q.get_result("r1", tenant="user-10") is None
        assert q.get_result("r2", tenant="user-10") is None
        assert q.get_result("r3", tenant="user-20") is not None

    def test_invalidate_judge_results_for_document(self, fake_redis_client):
        """invalidate_judge_results_for_document evicts entries whose contexts include the file."""
        import json
        q = make_queue()
        # Write directly with matching source path.
        fake_redis_client.set(
            "test:result:user-7:trace-a",
            json.dumps({"contexts": ["/data/tenants/user-7/corpus/report.pdf"], "status": "done"}),
        )
        # Another entry for same tenant but different file.
        fake_redis_client.set(
            "test:result:user-7:trace-b",
            json.dumps({"contexts": ["/data/tenants/user-7/corpus/other.txt"], "status": "done"}),
        )
        deleted = q.invalidate_judge_results_for_document("user-7", "report.pdf")
        assert deleted == 1
        # The matching entry must be gone.
        assert fake_redis_client.get("test:result:user-7:trace-a") is None
        # The unrelated entry must survive.
        assert fake_redis_client.get("test:result:user-7:trace-b") is not None
