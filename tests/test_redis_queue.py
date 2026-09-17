"""Tests for Phase 5 Part 2: Redis judge queue (fakeredis-based).

All 8 cases use fakeredis so no live Redis is needed.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from src.judge.redis_queue import JudgeQueue, _set_client


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
