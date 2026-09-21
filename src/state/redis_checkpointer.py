"""Redis checkpointer — drop-in replacement for SQLiteCheckpointer.

Implements the same interface as src.rag.checkpointer.SQLiteCheckpointer:
  save_state(thread_id, state)   → None
  load_state(thread_id)          → dict | None
  delete_state(thread_id)        → bool

State is stored as JSON under key "checkpoint:<thread_id>" with a TTL
(default CHECKPOINTER_TTL_SECONDS = 86400, i.e. 24 h) so old threads are
cleaned up automatically without a separate eviction job.

Selected by CHECKPOINTER_BACKEND="redis" at startup (see checkpointer_factory()
below).  Falls back gracefully to None-returns when Redis is unreachable so the
caller (A2A supervisor) can start a fresh workflow rather than crash.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_KEY_PREFIX = "checkpoint:"
_redis_client = None


def _get_client():
    global _redis_client
    if _redis_client is None:
        try:
            import redis as _r
            from src.rag.config import REDIS_URL
            _redis_client = _r.Redis.from_url(REDIS_URL, decode_responses=True)
        except Exception as exc:
            logger.warning("RedisCheckpointer: client init failed: %s", exc)
    return _redis_client


def _inject_client(client) -> None:
    """Test hook — inject a fakeredis client."""
    global _redis_client
    _redis_client = client


class RedisCheckpointer:
    """Durable, TTL-backed agent-state store backed by Redis."""

    def __init__(self, ttl: int | None = None, key_prefix: str = _KEY_PREFIX) -> None:
        from src.rag.config import CHECKPOINTER_TTL_SECONDS
        self._ttl = ttl if ttl is not None else CHECKPOINTER_TTL_SECONDS
        self._prefix = key_prefix

    def _key(self, thread_id: str) -> str:
        return self._prefix + thread_id

    def save_state(self, thread_id: str, state: dict) -> None:
        client = _get_client()
        if client is None:
            logger.warning("RedisCheckpointer: Redis unavailable — save_state skipped for %s", thread_id)
            return
        try:
            client.setex(self._key(thread_id), self._ttl, json.dumps(state, default=str))
        except Exception as exc:
            logger.error("RedisCheckpointer: save_state failed for %s: %s", thread_id, exc)

    def load_state(self, thread_id: str) -> dict | None:
        client = _get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._key(thread_id))
            if raw is None:
                return None
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("RedisCheckpointer: corrupt JSON for %s: %s", thread_id, exc)
            return None
        except Exception as exc:
            logger.error("RedisCheckpointer: load_state failed for %s: %s", thread_id, exc)
            return None

    def delete_state(self, thread_id: str) -> bool:
        client = _get_client()
        if client is None:
            return False
        try:
            deleted = client.delete(self._key(thread_id))
            return bool(deleted)
        except Exception as exc:
            logger.error("RedisCheckpointer: delete_state failed for %s: %s", thread_id, exc)
            return False

    def delete_tenant_checkpoints(self, tenant: str) -> int:
        """Delete all checkpoints belonging to tenant by SCAN + JSON filter.

        Redis keys have no tenant component (they are keyed by thread_id), so we
        must SCAN the full checkpoint keyspace and inspect the JSON body of each
        entry.  This is linear in the number of checkpoints but checkpoints are
        short-lived TTL keys so the keyspace is bounded.  Returns count deleted.
        """
        client = _get_client()
        if client is None:
            return 0
        count = 0
        pattern = self._prefix + "*"
        try:
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor, match=pattern, count=100)
                for key in keys:
                    try:
                        raw = client.get(key)
                        if raw is None:
                            continue
                        state = json.loads(raw)
                        if state.get("tenant_slug") == tenant:
                            client.delete(key)
                            count += 1
                    except Exception:
                        pass
                if cursor == 0:
                    break
        except Exception as exc:
            logger.error("RedisCheckpointer: delete_tenant_checkpoints failed for %s: %s", tenant, exc)
        return count


def checkpointer_factory():
    """Return the configured checkpointer instance.

    CHECKPOINTER_BACKEND="redis" → RedisCheckpointer
    CHECKPOINTER_BACKEND="sqlite" (default) → SQLiteCheckpointer
    """
    from src.rag.config import CHECKPOINTER_BACKEND
    if CHECKPOINTER_BACKEND == "redis":
        return RedisCheckpointer()
    from src.rag.checkpointer import SQLiteCheckpointer
    return SQLiteCheckpointer()
