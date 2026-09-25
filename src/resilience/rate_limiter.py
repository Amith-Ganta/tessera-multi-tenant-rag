"""Fixed-window rate limiter backed by Redis INCR/EXPIRE.

Uses a fixed-window approximation: one Redis key per (identifier, minute).
The INCR and EXPIRE are issued as a pipeline to eliminate the race condition
where a key could expire between INCR and EXPIRE, causing the TTL to never
be set.  EXPIRE is always sent; the cost is one extra round-trip per first
request per window, which is negligible.

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

    _UNSET = object()  # sentinel: client not yet initialised

    def __init__(self, limit: int | None = None, key_prefix: str = "rl:", redis_url: str | None = None) -> None:
        from src.rag.config import RATE_LIMIT_PER_MINUTE, REDIS_URL
        self._limit = limit if limit is not None else RATE_LIMIT_PER_MINUTE
        self._prefix = key_prefix
        self._redis_url = redis_url or REDIS_URL
        self._client = self._UNSET  # not yet initialised

    def _get_client(self):
        if self._client is self._UNSET:
            try:
                import redis as _r
                self._client = _r.Redis.from_url(self._redis_url, decode_responses=True)
            except Exception as exc:
                logger.warning("RateLimiter: Redis init failed: %s", exc)
                self._client = None
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
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, 60)
            count, _ = pipe.execute()
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
