"""Semantic answer cache with TTL eviction.

Cache key: sha256(query + "|" + "|".join(sorted(chunk_ids)) + "|" + model_name)

Write policy:
  - async JUDGE_MODE: cache.set() is called by the judge task AFTER the judge
    completes and its status is "done" (the async_runner must call it explicitly).
  - sync JUDGE_MODE: cache.set() is called by /ask after guard and judge both pass.
  - A failed guard response is NEVER cached.

Eviction: TTL-based. A background thread runs every 5 minutes and deletes entries
whose insertion time exceeds CACHE_TTL_SECONDS. Thread is daemon so it does not
block process shutdown.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

from src.rag.config import CACHE_TTL_SECONDS


class SemanticCache:
    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS, cleanup_interval: int = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[dict[str, Any], float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._cleanup_interval = cleanup_interval
        self._start_cleanup_thread()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(query: str, chunk_ids: list[str], model_name: str, tenant: str = "") -> str:
        # tenant MUST be first so different tenants never share a cache slot.
        raw = tenant + "|" + query + "|" + "|".join(sorted(chunk_ids)) + "|" + model_name
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            payload, inserted_at = entry
            if time.monotonic() - inserted_at > self._ttl:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return payload

    def set(self, key: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._store[key] = (payload, time.monotonic())

    def hit_rate(self) -> float:
        with self._lock:
            total = self._hits + self._misses
            return self._hits / total if total > 0 else 0.0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
                "hit_rate": self._hits / total if total > 0 else 0.0,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, ts) in self._store.items() if now - ts > self._ttl]
            for k in expired:
                del self._store[k]

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._cleanup_interval)
            self._evict_expired()

    def _start_cleanup_thread(self) -> None:
        t = threading.Thread(target=self._cleanup_loop, daemon=True, name="cache-cleanup")
        t.start()


# Process-wide singleton.
semantic_cache = SemanticCache()
