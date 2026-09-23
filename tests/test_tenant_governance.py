"""Tests for TenantGovernor — Phase 3B per-tenant resource governance.

All tests use fakeredis so no real Redis is needed.
"""

import fakeredis
import pytest

from src.resilience.tenant_governance import TenantGovernor


@pytest.fixture()
def governor():
    g = TenantGovernor()
    g.inject_client(fakeredis.FakeRedis(decode_responses=True))
    return g


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

class TestTokenBudget:
    def test_within_budget_returns_true(self, governor):
        assert governor.check_token_budget("tenant-a", 100) is True

    def test_exceeds_budget_returns_false(self, governor):
        from src.rag.config import TENANT_DAILY_TOKEN_BUDGET
        # consume the entire budget first
        governor.check_token_budget("tenant-b", TENANT_DAILY_TOKEN_BUDGET)
        # one more token pushes it over
        result = governor.check_token_budget("tenant-b", 1)
        assert result is False

    def test_fail_closed_on_redis_error(self):
        g = TenantGovernor(redis_url="redis://127.0.0.1:0/0")
        # Connection to port 0 will fail immediately
        result = g.check_token_budget("tenant-x", 10)
        assert result is False


# ---------------------------------------------------------------------------
# Concurrent request limit
# ---------------------------------------------------------------------------

class TestConcurrentLimit:
    def test_acquire_within_limit_returns_true(self, governor):
        assert governor.acquire_concurrent("tenant-c") is True

    def test_acquire_exceeding_limit_returns_false(self, governor):
        from src.rag.config import TENANT_MAX_CONCURRENT
        # fill up all slots
        for _ in range(TENANT_MAX_CONCURRENT):
            ok = governor.acquire_concurrent("tenant-d")
            assert ok is True
        # one more should be rejected
        assert governor.acquire_concurrent("tenant-d") is False

    def test_release_allows_new_acquire(self, governor):
        from src.rag.config import TENANT_MAX_CONCURRENT
        for _ in range(TENANT_MAX_CONCURRENT):
            governor.acquire_concurrent("tenant-e")
        # all slots full; release one
        governor.release_concurrent("tenant-e")
        assert governor.acquire_concurrent("tenant-e") is True


# ---------------------------------------------------------------------------
# Judge quota
# ---------------------------------------------------------------------------

class TestJudgeQuota:
    def test_within_quota_returns_true(self, governor):
        assert governor.check_judge_quota("tenant-f") is True

    def test_exceeds_quota_returns_false(self, governor):
        from src.rag.config import TENANT_DAILY_JUDGE_QUOTA
        # consume the entire quota
        for _ in range(TENANT_DAILY_JUDGE_QUOTA):
            governor.check_judge_quota("tenant-g")
        # next call should be rejected
        assert governor.check_judge_quota("tenant-g") is False
