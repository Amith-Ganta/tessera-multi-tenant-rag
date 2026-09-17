"""Redis-backed judge job queue (Phase 5, Pattern 4: Message Queue).

Replaces the in-process asyncio.create_task approach with a durable queue so
that judge jobs survive API process restarts, are bounded in concurrency, and
can be consumed by a separate judge_worker.py process.

All values are JSON-serialised before LPUSH / SET and deserialised on read.
The Redis client is thread-safe; a single module-level client is shared across
all callers in the same process.  Connectivity failures degrade gracefully:
publish() logs and returns without raising; get_result() returns None.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_redis_client = None


def _get_client():
    """Return (or lazily create) the shared Redis client."""
    global _redis_client
    if _redis_client is None:
        try:
            import redis as _redis_mod
            from src.rag.config import REDIS_URL
            _redis_client = _redis_mod.Redis.from_url(REDIS_URL, decode_responses=True)
        except Exception as exc:
            logger.warning("Redis client init failed: %s", exc)
            _redis_client = None
    return _redis_client


def _set_client(client) -> None:
    """Inject a pre-built client (used by tests via fakeredis)."""
    global _redis_client
    _redis_client = client


class JudgeQueue:
    """Thin wrapper around Redis list operations for the judge job queue."""

    def __init__(self, queue_name: str | None = None, results_prefix: str | None = None,
                 max_depth: int | None = None) -> None:
        from src.rag.config import JUDGE_QUEUE_NAME, JUDGE_RESULTS_PREFIX, JUDGE_QUEUE_MAX_DEPTH
        self._queue = queue_name or JUDGE_QUEUE_NAME
        self._prefix = results_prefix or JUDGE_RESULTS_PREFIX
        self._max_depth = max_depth if max_depth is not None else JUDGE_QUEUE_MAX_DEPTH

    # ── publishing ────────────────────────────────────────────────────────────

    def publish(self, trace_id: str, payload: dict[str, Any]) -> bool:
        """Push a job onto the left end of the queue. Returns True on success."""
        client = _get_client()
        if client is None:
            logger.error("Redis unavailable — judge job %s not queued", trace_id)
            return False
        try:
            data = json.dumps({"trace_id": trace_id, **payload})
            client.lpush(self._queue, data)
            return True
        except Exception as exc:
            logger.error("publish failed for trace_id=%s: %s", trace_id, exc)
            return False

    # ── result store ──────────────────────────────────────────────────────────

    def get_result(self, trace_id: str) -> dict | None:
        """Return the stored result dict, or None if absent / Redis down."""
        client = _get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._prefix + trace_id)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.error("get_result failed for trace_id=%s: %s", trace_id, exc)
            return None

    def set_result(self, trace_id: str, result: dict[str, Any], ttl: int = 86400) -> None:
        """Persist a result dict with a TTL (default 24 h)."""
        client = _get_client()
        if client is None:
            return
        try:
            client.setex(self._prefix + trace_id, ttl, json.dumps(result))
        except Exception as exc:
            logger.error("set_result failed for trace_id=%s: %s", trace_id, exc)

    # ── capacity ──────────────────────────────────────────────────────────────

    def queue_depth(self) -> int:
        """Return current queue depth; 0 if Redis is unreachable."""
        client = _get_client()
        if client is None:
            return 0
        try:
            return int(client.llen(self._queue))
        except Exception:
            return 0

    def is_over_capacity(self) -> bool:
        return self.queue_depth() >= self._max_depth

    # ── worker-side pop ───────────────────────────────────────────────────────

    def blocking_pop(self, timeout: int = 5) -> dict | None:
        """BRPOP with timeout; returns deserialised job dict or None on timeout."""
        client = _get_client()
        if client is None:
            return None
        try:
            result = client.brpop(self._queue, timeout=timeout)
            if result is None:
                return None
            _key, raw = result
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("malformed JSON in queue: %s", exc)
            return None
        except Exception as exc:
            logger.error("blocking_pop error: %s", exc)
            return None


# Module-level singleton used by the FastAPI app and the worker
judge_queue = JudgeQueue()
