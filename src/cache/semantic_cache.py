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

    def invalidate_by_document(self, tenant: str, filename: str) -> int:
        """Remove cache entries whose chunk IDs reference the given document.

        Cache keys are SHA-256 hashes so we cannot inspect them directly.  The
        cache stores the raw payload dict, which includes a ``sources`` field
        whose values are LangChain ``source`` metadata strings — absolute paths
        ending in ``/<filename>``.  We scan entries and evict any whose
        ``sources`` list contains a path that ends with the target filename.

        Returns the number of entries removed.
        """
        removed = 0
        target = filename if not filename.startswith("/") and not filename.startswith("\\") else filename.split("/")[-1].split("\\")[-1]
        with self._lock:
            keys_to_remove = []
            for key, (payload, _ts) in self._store.items():
                sources = payload.get("sources", []) or []
                if any(
                    str(s).endswith("/" + target) or
                    str(s).endswith("\\" + target) or
                    str(s) == target
                    for s in sources
                ):
                    keys_to_remove.append(key)
            for k in keys_to_remove:
                del self._store[k]
                removed += 1
        return removed

    def invalidate_by_tenant(self, tenant: str) -> int:
        """Remove all cache entries belonging to a tenant. Returns count removed.

        SemanticCache.make_key() embeds the tenant as the first segment of the
        pre-hash input but the stored key is a SHA-256 hex digest — we cannot
        reverse it.  The payload dict carries no tenant field either, so the
        only safe approach is to evict entries whose payload has a ``tenant``
        field equal to the target, or to fall back to clearing the whole cache
        when no tenant tag is stored.  We tag the payload at write time (via
        SemanticCache.set_tagged) when a tenant is known; callers that use the
        bare set() API will not have a tag and are not cleared here.
        """
        removed = 0
        with self._lock:
            keys_to_remove = [
                k for k, (payload, _ts) in self._store.items()
                if payload.get("_tenant") == tenant
            ]
            for k in keys_to_remove:
                del self._store[k]
                removed += 1
        return removed

    def set_tagged(self, key: str, payload: dict, tenant: str) -> None:
        """Like set(), but embeds _tenant in the payload for later invalidation."""
        tagged = dict(payload)
        tagged["_tenant"] = tenant
        with self._lock:
            self._store[key] = (tagged, time.monotonic())

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
