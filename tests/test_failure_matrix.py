"""Item 11: Dependency failure matrix classification tests.

Each test class corresponds to one row in docs/DEPENDENCY_FAILURE_MATRIX.md.
Tests verify the DOCUMENTED behaviour, not just that the code runs —
e.g. rate-limiter fail-open, circuit breaker transitions, bulkhead exhaustion.

No real Redis, LLM, or HTTP calls are made.
"""

from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# 1.1 Circuit Breaker state machine
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def _make(self, threshold: int = 3, recovery: int = 30) -> "CircuitBreaker":
        from src.resilience.circuit_breaker import CircuitBreaker
        return CircuitBreaker("test-breaker", failure_threshold=threshold, recovery_seconds=recovery)

    def test_initial_state_is_closed(self):
        cb = self._make()
        assert cb.state == "CLOSED"

    def test_opens_after_threshold_consecutive_failures(self):
        cb = self._make(threshold=3)
        for _ in range(3):
            cb.on_failure()
        assert cb.state == "OPEN"

    def test_stays_closed_under_threshold(self):
        cb = self._make(threshold=3)
        cb.on_failure()
        cb.on_failure()
        assert cb.state == "CLOSED"

    def test_raises_circuit_open_error_when_open(self):
        from src.resilience.circuit_breaker import CircuitOpenError
        cb = self._make(threshold=1)
        cb.on_failure()
        with pytest.raises(CircuitOpenError):
            cb.before_call()

    def test_transitions_to_half_open_after_recovery_window(self):
        cb = self._make(threshold=1, recovery=0)
        cb.on_failure()  # opens immediately
        assert cb.state == "OPEN"
        cb.before_call.__func__  # just access; actual test: time passes
        # Manually set _opened_at to the past
        cb._opened_at = time.time() - 1
        cb.before_call()  # should NOT raise — transitions to HALF_OPEN
        assert cb.state == "HALF_OPEN"

    def test_half_open_to_closed_on_success(self):
        cb = self._make(threshold=1, recovery=0)
        cb.on_failure()
        cb._opened_at = time.time() - 1
        try:
            cb.before_call()
        except Exception:
            pass
        cb.on_success()
        assert cb.state == "CLOSED"

    def test_half_open_to_open_on_failure(self):
        cb = self._make(threshold=1, recovery=0)
        cb.on_failure()
        cb._opened_at = time.time() - 1
        try:
            cb.before_call()
        except Exception:
            pass
        cb.on_failure()
        assert cb.state == "OPEN"

    def test_call_wraps_success(self):
        cb = self._make()
        result = cb.call(lambda: 42)
        assert result == 42
        assert cb.state == "CLOSED"

    def test_call_records_failure_and_reraises(self):
        cb = self._make(threshold=1)
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        assert cb.state == "OPEN"

    def test_failure_counter_resets_on_success(self):
        cb = self._make(threshold=5)
        cb.on_failure()
        cb.on_failure()
        cb.on_success()
        assert cb._failures == 0

    def test_reset_restores_closed_state(self):
        cb = self._make(threshold=1)
        cb.on_failure()
        assert cb.state == "OPEN"
        cb.reset()
        assert cb.state == "CLOSED"
        assert cb._failures == 0


# ---------------------------------------------------------------------------
# 1.2 Bulkhead
# ---------------------------------------------------------------------------

class TestBulkhead:
    def test_acquire_and_release_within_capacity(self):
        from src.resilience.bulkhead import Bulkhead
        bh = Bulkhead("test", capacity=2)
        bh.acquire()
        assert bh.active == 1
        bh.release()
        assert bh.active == 0

    def test_raises_when_capacity_exhausted(self):
        from src.resilience.bulkhead import Bulkhead, BulkheadFullError
        bh = Bulkhead("test", capacity=1)
        bh.acquire()
        with pytest.raises(BulkheadFullError):
            bh.acquire()

    def test_context_manager_releases_on_exit(self):
        from src.resilience.bulkhead import Bulkhead
        bh = Bulkhead("test", capacity=1)
        with bh:
            assert bh.active == 1
        assert bh.active == 0

    def test_context_manager_releases_on_exception(self):
        from src.resilience.bulkhead import Bulkhead
        bh = Bulkhead("test", capacity=1)
        try:
            with bh:
                raise RuntimeError("inside")
        except RuntimeError:
            pass
        assert bh.active == 0

    def test_call_executes_fn_within_bulkhead(self):
        from src.resilience.bulkhead import Bulkhead
        bh = Bulkhead("test", capacity=2)
        result = bh.call(lambda: "ok")
        assert result == "ok"
        assert bh.active == 0

    def test_bulkhead_error_message_contains_name_and_capacity(self):
        from src.resilience.bulkhead import Bulkhead, BulkheadFullError
        bh = Bulkhead("llm-pool", capacity=1)
        bh.acquire()
        with pytest.raises(BulkheadFullError) as exc_info:
            bh.acquire()
        msg = str(exc_info.value)
        assert "llm-pool" in msg
        assert "1" in msg


# ---------------------------------------------------------------------------
# 1.3 Rate limiter — fail-open when Redis is down
# ---------------------------------------------------------------------------

class TestRateLimiterFailOpen:
    def _make_limiter(self, limit: int = 5):
        from src.resilience.rate_limiter import RateLimiter
        rl = RateLimiter(limit=limit, redis_url="redis://127.0.0.1:6379")
        return rl

    def test_fail_open_when_redis_unavailable(self):
        """When Redis raises, check() returns 0 (pass-through, no RateLimitError)."""
        from src.resilience.rate_limiter import RateLimiter
        rl = self._make_limiter(limit=1)
        bad_client = MagicMock()
        pipe = MagicMock()
        bad_client.pipeline.return_value = pipe
        pipe.execute.side_effect = ConnectionError("Redis down")
        rl.inject_client(bad_client)
        # Should not raise RateLimitError even though limit=1
        result = rl.check("tenant-x")
        assert result == 0

    def test_limits_enforced_when_redis_works(self):
        """Normal path: exceeding the limit raises RateLimitError."""
        import fakeredis
        from src.resilience.rate_limiter import RateLimiter, RateLimitError
        rl = RateLimiter(limit=2, redis_url="redis://127.0.0.1:6379")
        rl.inject_client(fakeredis.FakeRedis(decode_responses=True))
        rl.check("t1")
        rl.check("t1")
        with pytest.raises(RateLimitError):
            rl.check("t1")

    def test_different_identifiers_are_independent(self):
        import fakeredis
        from src.resilience.rate_limiter import RateLimiter, RateLimitError
        rl = RateLimiter(limit=1, redis_url="redis://127.0.0.1:6379")
        rl.inject_client(fakeredis.FakeRedis(decode_responses=True))
        rl.check("tenant-a")
        with pytest.raises(RateLimitError):
            rl.check("tenant-a")
        # tenant-b should still be allowed
        result = rl.check("tenant-b")
        assert result == 1

    def test_returns_zero_when_redis_client_init_fails(self):
        """If Redis client cannot be constructed, check() returns 0 (fail-open)."""
        from src.resilience.rate_limiter import RateLimiter
        rl = RateLimiter(limit=1, redis_url="redis://invalid-host-xyz:6379")
        rl._client = None  # force re-init
        with patch("redis.Redis.from_url", side_effect=ConnectionError("cannot connect")):
            result = rl.check("tenant-fail")
        assert result == 0


# ---------------------------------------------------------------------------
# 1.4 Judge queue: publish failure → honest status (not silent drop)
# ---------------------------------------------------------------------------

class TestJudgeQueuePublishFailure:
    def test_publish_returns_false_when_redis_raises(self):
        """JudgeQueue.publish() returns False if Redis raises on lpush."""
        from src.judge.redis_queue import JudgeQueue, _set_client
        q = JudgeQueue()
        bad_client = MagicMock()
        bad_client.lpush.side_effect = ConnectionError("Redis down")
        _set_client(bad_client)
        try:
            result = q.publish("trace-123", {"question": "test", "tenant": "t1"})
            assert result is False
        finally:
            _set_client(None)  # restore clean state

    def test_blocking_pop_returns_none_when_redis_down(self):
        """blocking_pop() returns None on Redis error (brpop raises)."""
        from src.judge.redis_queue import JudgeQueue, _set_client
        q = JudgeQueue()
        bad_client = MagicMock()
        bad_client.brpop.side_effect = ConnectionError("Redis down")
        _set_client(bad_client)
        try:
            result = q.blocking_pop(timeout=0)
            assert result is None
        finally:
            _set_client(None)


# ---------------------------------------------------------------------------
# 1.5 Failure matrix document exists and is structurally valid
# ---------------------------------------------------------------------------

class TestFailureMatrixDocument:
    def test_document_exists(self):
        from pathlib import Path
        doc = Path(__file__).parents[1] / "docs" / "DEPENDENCY_FAILURE_MATRIX.md"
        assert doc.exists(), "DEPENDENCY_FAILURE_MATRIX.md is missing"

    def test_document_covers_expected_dependencies(self):
        from pathlib import Path
        doc = Path(__file__).parents[1] / "docs" / "DEPENDENCY_FAILURE_MATRIX.md"
        text = doc.read_text(encoding="utf-8")
        for topic in ("Circuit Breaker", "Bulkhead", "Redis", "Rate Limiter", "LLM Provider"):
            assert topic.lower() in text.lower(), (
                f"DEPENDENCY_FAILURE_MATRIX.md must document {topic}"
            )

    def test_document_has_summary_matrix_section(self):
        from pathlib import Path
        doc = Path(__file__).parents[1] / "docs" / "DEPENDENCY_FAILURE_MATRIX.md"
        text = doc.read_text(encoding="utf-8")
        assert "Summary Matrix" in text, (
            "DEPENDENCY_FAILURE_MATRIX.md must have a Summary Matrix section"
        )

    def test_all_mismatches_are_documented(self):
        from pathlib import Path
        doc = Path(__file__).parents[1] / "docs" / "DEPENDENCY_FAILURE_MATRIX.md"
        text = doc.read_text(encoding="utf-8")
        # MM-01 and MM-02 should be marked FIXED; MM-03 accepted
        assert "MM-01" in text
        assert "MM-02" in text
        assert "MM-03" in text
        assert "FIXED" in text or "fixed" in text
