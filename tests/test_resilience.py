"""Tests for Phase 5 Part 3: Rate limiter, circuit breaker, and bulkhead."""

from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from src.resilience.rate_limiter import RateLimiter, RateLimitError
from src.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.resilience.bulkhead import Bulkhead, BulkheadFullError


# ---------------------------------------------------------------------------
# Rate limiter (4 cases)
# ---------------------------------------------------------------------------

class TestRateLimiter:
    @pytest.fixture
    def rl(self):
        limiter = RateLimiter(limit=5, key_prefix="test_rl:")
        limiter.inject_client(fakeredis.FakeRedis(decode_responses=True))
        return limiter

    def test_allows_requests_under_limit(self, rl):
        for _ in range(5):
            count = rl.check("user1")
        assert count == 5

    def test_raises_on_exceed(self, rl):
        for _ in range(5):
            rl.check("user2")
        with pytest.raises(RateLimitError) as exc_info:
            rl.check("user2")
        assert exc_info.value.limit == 5
        assert exc_info.value.current == 6

    def test_independent_identifiers(self, rl):
        for _ in range(5):
            rl.check("user-a")
        # user-b should still be under limit
        count = rl.check("user-b")
        assert count == 1

    def test_fail_open_when_redis_unavailable(self):
        rl = RateLimiter(limit=1, key_prefix="test_rl_down:")
        rl.inject_client(None)  # simulate Redis down
        # Should not raise even if called many times
        for _ in range(10):
            count = rl.check("user-x")
        assert count == 0  # fail-open returns 0


# ---------------------------------------------------------------------------
# Circuit breaker (5 cases)
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    @pytest.fixture
    def cb(self):
        breaker = CircuitBreaker("test-cb", failure_threshold=3, recovery_seconds=60)
        return breaker

    def test_starts_closed(self, cb):
        assert cb.state == "CLOSED"

    def test_opens_after_threshold_failures(self, cb):
        for _ in range(3):
            cb.on_failure()
        assert cb.state == "OPEN"

    def test_raises_when_open(self, cb):
        for _ in range(3):
            cb.on_failure()
        with pytest.raises(CircuitOpenError):
            cb.before_call()

    def test_transitions_to_half_open_after_recovery(self):
        cb = CircuitBreaker("test-cb-recovery", failure_threshold=1, recovery_seconds=0)
        cb.on_failure()
        assert cb.state == "OPEN"
        time.sleep(0.01)
        cb.before_call()  # should NOT raise — transitions to HALF_OPEN
        assert cb.state == "HALF_OPEN"

    def test_closes_on_success_in_half_open(self):
        cb = CircuitBreaker("test-cb-close", failure_threshold=1, recovery_seconds=0)
        cb.on_failure()
        time.sleep(0.01)
        cb.before_call()  # → HALF_OPEN
        cb.on_success()
        assert cb.state == "CLOSED"

    def test_call_records_failure_and_reraises(self, cb):
        def bad_fn():
            raise ValueError("broken")

        with pytest.raises(ValueError):
            cb.call(bad_fn)
        assert cb._failures == 1


# ---------------------------------------------------------------------------
# Bulkhead (3 cases)
# ---------------------------------------------------------------------------

class TestBulkhead:
    def test_allows_up_to_capacity(self):
        bh = Bulkhead("test-bh", capacity=3)
        bh.acquire()
        bh.acquire()
        bh.acquire()
        assert bh.active == 3
        bh.release()
        bh.release()
        bh.release()

    def test_raises_when_full(self):
        bh = Bulkhead("test-bh-full", capacity=2)
        bh.acquire()
        bh.acquire()
        with pytest.raises(BulkheadFullError):
            bh.acquire()
        bh.release()
        bh.release()

    def test_context_manager_releases_on_exit(self):
        bh = Bulkhead("test-bh-ctx", capacity=1)
        with bh:
            assert bh.active == 1
        assert bh.active == 0
