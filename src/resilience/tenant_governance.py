"""Per-tenant resource governance: token budget, concurrency, and judge quota.

All methods are fail-CLOSED: any Redis error is treated as a limit breach so
that a degraded Redis cluster cannot be exploited to bypass tenant fairness
boundaries.

When REDIS_URL is not explicitly set in the environment, the app uses
NullTenantGovernor (passthrough) so that test environments without Redis are
not blocked.  Production always sets REDIS_URL explicitly.
"""

from __future__ import annotations

import datetime
import logging

import redis as _redis

from src.rag.config import (
    REDIS_URL,
    TENANT_DAILY_JUDGE_QUOTA,
    TENANT_DAILY_TOKEN_BUDGET,
    TENANT_MAX_CONCURRENT,
)

logger = logging.getLogger(__name__)


class NullTenantGovernor:
    """No-op governor for environments without Redis (e.g. unit tests).

    All checks pass; release is a no-op.  Used when REDIS_URL is not
    explicitly configured so that tests without a Redis instance are not
    blocked by fail-closed logic.
    """

    def check_token_budget(self, tenant: str, tokens_used: int) -> bool:  # noqa: ARG002
        return True

    def acquire_concurrent(self, tenant: str) -> bool:  # noqa: ARG002
        return True

    def release_concurrent(self, tenant: str) -> None:  # noqa: ARG002
        return

    def check_judge_quota(self, tenant: str) -> bool:  # noqa: ARG002
        return True

    def inject_client(self, client: object) -> None:  # noqa: ARG002
        return


def _today() -> str:
    return datetime.date.today().strftime("%Y%m%d")


class TenantGovernor:
    """Fail-closed per-tenant resource governor backed by Redis."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._url = redis_url or REDIS_URL
        self._client: _redis.Redis | None = None

    def _get_client(self) -> _redis.Redis:
        if self._client is None:
            self._client = _redis.from_url(self._url, protocol=2, decode_responses=True)
        return self._client

    def inject_client(self, client: _redis.Redis) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Token budget
    # ------------------------------------------------------------------

    def check_token_budget(self, tenant: str, tokens_used: int) -> bool:
        """Return True if the tenant is within their daily token budget.

        Increments the counter by *tokens_used*.  Fail-closed on any error.
        """
        try:
            client = self._get_client()
            key = f"tokens:{tenant}:{_today()}"
            new_total = client.incrby(key, tokens_used)
            client.expire(key, 172800)  # 2 days TTL
            if new_total > TENANT_DAILY_TOKEN_BUDGET:
                logger.warning(
                    "tenant %s exceeded token budget: %d > %d",
                    tenant, new_total, TENANT_DAILY_TOKEN_BUDGET,
                )
                return False
            return True
        except Exception as exc:
            logger.error("TenantGovernor.check_token_budget fail-closed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Concurrent request limit
    # ------------------------------------------------------------------

    def acquire_concurrent(self, tenant: str) -> bool:
        """Increment the in-flight counter.  Return False if limit exceeded.

        Atomically decrements if the limit is breached so the counter stays
        accurate.  Fail-closed on any error.
        """
        try:
            client = self._get_client()
            key = f"concurrent:{tenant}"
            new_val = client.incr(key)
            if new_val > TENANT_MAX_CONCURRENT:
                client.decr(key)
                logger.warning(
                    "tenant %s exceeded concurrent limit: %d > %d",
                    tenant, new_val, TENANT_MAX_CONCURRENT,
                )
                return False
            return True
        except Exception as exc:
            logger.error("TenantGovernor.acquire_concurrent fail-closed: %s", exc)
            return False

    def release_concurrent(self, tenant: str) -> None:
        """Decrement the in-flight counter, floored at 0."""
        try:
            client = self._get_client()
            key = f"concurrent:{tenant}"
            current = client.get(key)
            if current is not None and int(current) > 0:
                client.decr(key)
        except Exception as exc:
            logger.error("TenantGovernor.release_concurrent error: %s", exc)

    # ------------------------------------------------------------------
    # Judge quota
    # ------------------------------------------------------------------

    def check_judge_quota(self, tenant: str) -> bool:
        """Return True if the tenant is within their daily judge quota.

        Increments the counter.  Fail-closed on any error.
        """
        try:
            client = self._get_client()
            key = f"judge_quota:{tenant}:{_today()}"
            new_total = client.incr(key)
            client.expire(key, 172800)  # 2 days TTL
            if new_total > TENANT_DAILY_JUDGE_QUOTA:
                logger.warning(
                    "tenant %s exceeded judge quota: %d > %d",
                    tenant, new_total, TENANT_DAILY_JUDGE_QUOTA,
                )
                return False
            return True
        except Exception as exc:
            logger.error("TenantGovernor.check_judge_quota fail-closed: %s", exc)
            return False
