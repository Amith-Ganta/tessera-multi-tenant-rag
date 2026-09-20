"""Regression tests for the three most critical unmitigated dependency failures.

These tests verify that the documented failure behaviour matches the code, and
that no silent regression changes a known-unsafe behaviour to a dangerous one
without detection.

Failures covered (from docs/DEPENDENCY_FAILURE_MATRIX.md):
  1. MM-01 — Embedding failure propagates as an unhandled exception (no retry/fallback).
  2. MM-02 — Judge-queue publish failure is silent: caller gets eval=pending even when
             the job was never queued.
  3. Partial MM-03 — Rate-limiter fail-open: Redis down → all requests allowed through.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# MM-01: OpenAI Embedding failure is unmitigated
# ---------------------------------------------------------------------------

class TestEmbeddingFailureUnmitigated:
    """An embedding API failure propagates as an exception with no fallback."""

    def test_embed_query_exception_propagates(self, monkeypatch):
        """When embed_query raises, the exception reaches the caller unchanged."""
        import types

        def _raise_embed(_q):
            raise RuntimeError("embedding API unavailable")

        fake_vs = types.SimpleNamespace(
            embeddings=types.SimpleNamespace(embed_query=_raise_embed),
            similarity_search_by_vector=lambda *a, **kw: [],
        )

        from src.rag import retriever_dense
        monkeypatch.setattr(retriever_dense, "get_vectorstore", lambda: fake_vs)

        with pytest.raises(RuntimeError, match="embedding API unavailable"):
            retriever_dense.retrieve("what is the policy?")

    def test_retrieve_with_scores_exception_propagates(self, monkeypatch):
        """retrieve_with_scores also has no fallback on embedding failure."""
        import types

        def _raise_embed(_q):
            raise ConnectionError("network timeout")

        fake_vs = types.SimpleNamespace(
            embeddings=types.SimpleNamespace(embed_query=_raise_embed),
            similarity_search_by_vector_with_relevance_scores=lambda *a, **kw: [],
        )

        from src.rag import retriever_dense
        monkeypatch.setattr(retriever_dense, "get_vectorstore", lambda: fake_vs)

        with pytest.raises(ConnectionError, match="network timeout"):
            retriever_dense.retrieve_with_scores("what is the policy?")

    def test_no_retry_on_embedding_failure(self, monkeypatch):
        """The embedding call is made exactly once — no retry logic wraps it."""
        import types

        call_count = {"n": 0}

        def _failing_embed(q):
            call_count["n"] += 1
            raise RuntimeError("embed failed")

        fake_vs = types.SimpleNamespace(
            embeddings=types.SimpleNamespace(embed_query=_failing_embed),
            similarity_search_by_vector=lambda *a, **kw: [],
        )

        from src.rag import retriever_dense
        monkeypatch.setattr(retriever_dense, "get_vectorstore", lambda: fake_vs)

        with pytest.raises(RuntimeError):
            retriever_dense.retrieve("test question")

        assert call_count["n"] == 1, (
            f"Expected exactly 1 embedding call (no retry), got {call_count['n']}"
        )


# ---------------------------------------------------------------------------
# MM-02: Judge-queue publish failure is silent
# ---------------------------------------------------------------------------

class TestJudgeQueuePublishSilentDrop:
    """When Redis is unavailable, publish() returns False and drops the job silently."""

    def test_publish_returns_false_when_redis_down(self, monkeypatch):
        """publish() returns False (not raises) when Redis is unreachable."""
        import src.judge.redis_queue as rq

        monkeypatch.setattr(rq, "_get_client", lambda: None)

        q = rq.JudgeQueue(queue_name="test:q", results_prefix="test:r:", max_depth=10)
        result = q.publish("trace-001", {"question": "q", "answer": "a", "contexts": []})

        assert result is False, "publish() must return False (not raise) when Redis is down"

    def test_publish_false_does_not_raise(self, monkeypatch):
        """The caller of publish() sees no exception even when Redis is completely down."""
        import src.judge.redis_queue as rq

        monkeypatch.setattr(rq, "_get_client", lambda: None)

        q = rq.JudgeQueue(queue_name="test:q", results_prefix="test:r:", max_depth=10)
        try:
            q.publish("trace-002", {"data": "payload"})
        except Exception as exc:
            pytest.fail(
                f"publish() raised unexpectedly when Redis is down: {exc!r}"
            )

    def test_get_result_returns_none_when_redis_down(self, monkeypatch):
        """get_result() returns None (not raises) when Redis is unreachable."""
        import src.judge.redis_queue as rq

        monkeypatch.setattr(rq, "_get_client", lambda: None)

        q = rq.JudgeQueue(queue_name="test:q", results_prefix="test:r:", max_depth=10)
        result = q.get_result("trace-003")

        assert result is None, "get_result() must return None (not raise) when Redis is down"

    def test_dropped_job_leaves_no_result(self, monkeypatch):
        """After a failed publish, get_result returns None — caller cannot distinguish
        from a job that was queued but not yet processed."""
        import src.judge.redis_queue as rq

        monkeypatch.setattr(rq, "_get_client", lambda: None)

        q = rq.JudgeQueue(queue_name="test:q", results_prefix="test:r:", max_depth=10)
        trace_id = "trace-silent-drop"
        published = q.publish(trace_id, {"question": "q", "answer": "a"})
        result = q.get_result(trace_id)

        assert published is False
        assert result is None, (
            "MM-02: a dropped job is indistinguishable from a queued-but-unprocessed job; "
            "get_result returns None in both cases"
        )


# ---------------------------------------------------------------------------
# Partial MM-03: Rate-limiter fail-open when Redis is down
# ---------------------------------------------------------------------------

class TestRateLimiterFailOpen:
    """When Redis is unreachable the rate limiter must fail open (allow all requests)."""

    def _make_limiter_with_broken_redis(self):
        """Return a RateLimiter whose injected client raises on pipeline().execute()."""
        from src.resilience.rate_limiter import RateLimiter

        class _BrokenPipeline:
            def incr(self, *a, **kw): pass
            def expire(self, *a, **kw): pass
            def execute(self):
                raise ConnectionError("Redis unreachable")

        class _BrokenClient:
            def pipeline(self):
                return _BrokenPipeline()

        limiter = RateLimiter()
        limiter.inject_client(_BrokenClient())
        return limiter

    def test_check_does_not_raise_when_redis_down(self):
        """RateLimiter.check() must not raise when Redis pipeline raises."""
        limiter = self._make_limiter_with_broken_redis()

        try:
            limiter.check("tenant_abc")
        except Exception as exc:
            pytest.fail(
                f"RateLimiter.check() raised when Redis is down — should fail open: {exc!r}"
            )

    def test_check_allows_request_when_redis_down(self):
        """check() returning without RateLimitError means the request is allowed (fail-open)."""
        from src.resilience.rate_limiter import RateLimitError

        limiter = self._make_limiter_with_broken_redis()
        allowed = True
        try:
            limiter.check("tenant_xyz")
        except RateLimitError:
            allowed = False

        assert allowed, (
            "Rate limiter must fail open when Redis is unreachable: "
            "request must be allowed, not rate-limited"
        )

    def test_fail_open_returns_zero(self):
        """check() returns 0 (the fail-open sentinel) when Redis pipeline raises."""
        limiter = self._make_limiter_with_broken_redis()
        count = limiter.check("tenant_test")
        assert count == 0, (
            f"check() should return 0 on Redis failure (fail-open), got {count}"
        )

    def test_multiple_tenants_all_allowed_when_redis_down(self):
        """All tenants are allowed through when Redis is down (no partial enforcement)."""
        from src.resilience.rate_limiter import RateLimitError

        limiter = self._make_limiter_with_broken_redis()
        for tenant in ["tenant_a", "tenant_b", "tenant_c"]:
            try:
                limiter.check(tenant)
            except RateLimitError:
                pytest.fail(
                    f"tenant '{tenant}' was rate-limited despite Redis being down (must fail open)"
                )
