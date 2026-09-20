"""Redis-backed judge job queue (Phase 5, Pattern 4: Message Queue).

Replaces the in-process asyncio.create_task approach with a durable queue so
that judge jobs survive API process restarts, are bounded in concurrency, and
can be consumed by a separate judge_worker.py process.

Reliability model:
  - At-most-once semantics: BRPOP removes the job from the queue before
    processing; if the worker crashes mid-job, the job is lost.  This is
    intentional for the judge queue — a missing eval is recoverable (the
    result is marked "pending" indefinitely), whereas at-least-once delivery
    would require idempotency guarantees on the judge itself.
  - Retry counter: each job carries a ``_attempts`` field.  The worker
    increments it before processing and re-queues on failure (up to
    MAX_JOB_ATTEMPTS).  Jobs that exceed the retry limit are pushed to the
    DLQ (``<queue_name>:dlq``) so they can be inspected without blocking
    the main queue.
  - DLQ: ``judge:queue:dlq`` receives jobs that exhausted all retries, plus
    malformed payloads.  The DLQ is not consumed automatically; an operator
    drains it manually via the ``/admin/dlq`` endpoint or Redis CLI.

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

MAX_JOB_ATTEMPTS = 3

_redis_client = None


def _get_client():
    """Return (or lazily create) the shared Redis client.

    The client is created lazily on first use, not at module load, so importing
    this module never blocks on Redis.  Redis 7 defaults to RESP3, whose
    handshake can stall under Docker; forcing ``protocol=2`` with short socket
    timeouts makes failures fast and explicit instead of silently returning an
    unusable client.  If construction fails, the client is left as ``None`` so
    the next call retries rather than caching a dead handle.
    """
    global _redis_client
    if _redis_client is None:
        try:
            import redis as _redis_mod
            from src.rag.config import REDIS_URL
            _redis_client = _redis_mod.Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                protocol=2,
                socket_connect_timeout=2,
                socket_timeout=2,
                health_check_interval=30,
            )
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
        self._dlq = self._queue + ":dlq"
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
            client.set(self._prefix + trace_id, json.dumps(result), ex=ttl)
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
        """BRPOP with timeout; returns deserialised job dict or None on timeout.

        Increments the ``_attempts`` counter on the job before returning it so
        the worker knows how many times it has been tried.  Malformed payloads
        are pushed to the DLQ immediately rather than being dropped silently.
        """
        client = _get_client()
        if client is None:
            return None
        try:
            result = client.brpop(self._queue, timeout=timeout)
            if result is None:
                return None
            _key, raw = result
            job = json.loads(raw)
            job["_attempts"] = int(job.get("_attempts", 0)) + 1
            return job
        except json.JSONDecodeError as exc:
            logger.error("malformed JSON in queue — moving to DLQ: %s", exc)
            self._push_to_dlq(raw, reason=str(exc))
            return None
        except Exception as exc:
            logger.error("blocking_pop error: %s", exc)
            return None

    def requeue_or_dlq(self, job: dict[str, Any]) -> None:
        """Re-queue a failed job up to MAX_JOB_ATTEMPTS; route to DLQ thereafter.

        Call this from the worker when a job raises an exception.  The job's
        ``_attempts`` counter must already have been incremented by blocking_pop.
        """
        attempts = int(job.get("_attempts", 1))
        if attempts < MAX_JOB_ATTEMPTS:
            client = _get_client()
            if client is not None:
                try:
                    client.lpush(self._queue, json.dumps(job))
                    logger.warning(
                        "judge job %s re-queued (attempt %d/%d)",
                        job.get("trace_id"), attempts, MAX_JOB_ATTEMPTS,
                    )
                    return
                except Exception as exc:
                    logger.error("requeue failed for trace_id=%s: %s", job.get("trace_id"), exc)
        self._push_to_dlq(json.dumps(job), reason=f"exhausted {attempts} attempts")

    def _push_to_dlq(self, raw: str, *, reason: str) -> None:
        """Push a raw job string to the DLQ with an added failure reason."""
        client = _get_client()
        if client is None:
            logger.error("DLQ push skipped — Redis unavailable; reason: %s", reason)
            return
        try:
            import time as _time
            envelope = json.dumps({"_dlq_reason": reason, "_dlq_ts": _time.time(), "_raw": raw})
            client.lpush(self._dlq, envelope)
            logger.error("job pushed to DLQ (%s); reason: %s", self._dlq, reason)
        except Exception as exc:
            logger.error("DLQ push failed: %s", exc)

    def dlq_depth(self) -> int:
        """Return the current DLQ depth; 0 if Redis is unreachable."""
        client = _get_client()
        if client is None:
            return 0
        try:
            return int(client.llen(self._dlq))
        except Exception:
            return 0


# Module-level singleton used by the FastAPI app and the worker
judge_queue = JudgeQueue()
