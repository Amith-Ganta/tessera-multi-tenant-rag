"""Sliding-window rate limiter backed by Redis INCR/EXPIRE.

Uses a fixed-window approximation: one Redis key per (identifier, minute).
The key is created with INCR and given a 60 s TTL on the first increment of
each window; subsequent increments within the window raise RateLimitError
when the count exceeds the configured limit.

Degrades gracefully: if Redis is unreachable, all requests pass through
(fail-open) and a warning is logged.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """Raised when the per-minute limit is exceeded."""

    def __init__(self, identifier: str, limit: int, current: int) -> None:
        self.identifier = identifier
        self.limit = limit
        self.current = current
        super().__init__(f"rate limit exceeded for {identifier!r}: {current}/{limit} req/min")


class RateLimiter:
    """Per-identifier sliding-window rate limiter.

    Args:
        limit:      max requests per 60-second window.
        key_prefix: Redis key prefix (default "rl:").
        redis_url:  overrides REDIS_URL from config when provided.
    """

    def __init__(self, limit: int | None = None, key_prefix: str = "rl:", redis_url: str | None = None) -> None:
        from src.rag.config import RATE_LIMIT_PER_MINUTE, REDIS_URL
        self._limit = limit if limit is not None else RATE_LIMIT_PER_MINUTE
        self._prefix = key_prefix
        self._redis_url = redis_url or REDIS_URL
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import redis as _r
                self._client = _r.Redis.from_url(self._redis_url, decode_responses=True)
            except Exception as exc:
                logger.warning("RateLimiter: Redis init failed: %s", exc)
        return self._client

    def check(self, identifier: str) -> int:
        """Return the current request count; raises RateLimitError if over limit.

        The count is computed for a 60-second fixed window keyed by the current
        Unix minute (floor(time / 60)).  On Redis failure the call passes through
        and returns 0 (fail-open).
        """
        client = self._get_client()
        if client is None:
            return 0

        window = int(time.time() // 60)
        key = f"{self._prefix}{identifier}:{window}"
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, 60)
            if count > self._limit:
                raise RateLimitError(identifier, self._limit, count)
            return count
        except RateLimitError:
            raise
        except Exception as exc:
            logger.warning("RateLimiter: Redis error for %r — passing through: %s", identifier, exc)
            return 0

    def inject_client(self, client) -> None:
        """Inject a pre-built client (used by tests via fakeredis)."""
        self._client = client
