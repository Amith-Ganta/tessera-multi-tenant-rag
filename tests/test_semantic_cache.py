"""Tests for Phase 4b: semantic caching."""

from __future__ import annotations

import time

import pytest

from src.cache.semantic_cache import SemanticCache


class TestSemanticCacheMakeKey:
    def test_same_inputs_same_key(self):
        k1 = SemanticCache.make_key("hello", ["a", "b"], "model-x")
        k2 = SemanticCache.make_key("hello", ["a", "b"], "model-x")
        assert k1 == k2

    def test_sorted_chunk_ids_order_invariant(self):
        k1 = SemanticCache.make_key("q", ["c", "a", "b"], "m")
        k2 = SemanticCache.make_key("q", ["a", "b", "c"], "m")
        assert k1 == k2

    def test_different_query_different_key(self):
        k1 = SemanticCache.make_key("question A", ["x"], "m")
        k2 = SemanticCache.make_key("question B", ["x"], "m")
        assert k1 != k2

    def test_different_model_different_key(self):
        k1 = SemanticCache.make_key("q", ["x"], "model-a")
        k2 = SemanticCache.make_key("q", ["x"], "model-b")
        assert k1 != k2

    def test_different_chunks_different_key(self):
        k1 = SemanticCache.make_key("q", ["chunk-1"], "m")
        k2 = SemanticCache.make_key("q", ["chunk-2"], "m")
        assert k1 != k2

    def test_key_is_hex_sha256(self):
        k = SemanticCache.make_key("q", [], "m")
        assert len(k) == 64
        int(k, 16)  # raises ValueError if not valid hex


class TestSemanticCacheGetSet:
    def test_get_returns_none_before_set(self):
        cache = SemanticCache(ttl_seconds=60)
        assert cache.get("nonexistent") is None

    def test_set_then_get_returns_value(self):
        cache = SemanticCache(ttl_seconds=60)
        cache.set("k1", {"answer": "42"})
        assert cache.get("k1") == {"answer": "42"}

    def test_expired_entry_returns_none(self):
        cache = SemanticCache(ttl_seconds=1, cleanup_interval=9999)
        cache.set("k2", {"answer": "old"})
        time.sleep(1.05)
        assert cache.get("k2") is None

    def test_hit_rate_increments_correctly(self):
        cache = SemanticCache(ttl_seconds=60)
        cache.set("k", {"v": 1})
        cache.get("k")   # hit
        cache.get("k")   # hit
        cache.get("missing")  # miss
        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert abs(stats["hit_rate"] - 2 / 3) < 0.001

    def test_clear_resets_store_and_counters(self):
        cache = SemanticCache(ttl_seconds=60)
        cache.set("k", {"v": 1})
        cache.get("k")
        cache.clear()
        assert cache.get("k") is None
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 1  # the miss from the clear-then-get
        assert stats["size"] == 0

    def test_overwrite_key_updates_value(self):
        cache = SemanticCache(ttl_seconds=60)
        cache.set("k", {"v": 1})
        cache.set("k", {"v": 2})
        assert cache.get("k") == {"v": 2}

    def test_empty_hit_rate_is_zero(self):
        cache = SemanticCache(ttl_seconds=60)
        assert cache.hit_rate() == 0.0


class TestSemanticCacheTenantIsolation:
    """Regression tests for CVE: cross-tenant cache data leak (tenant missing from key)."""

    def test_different_tenants_different_keys(self):
        k1 = SemanticCache.make_key("same query", ["chunk-1"], "model-x", tenant="tenant_a")
        k2 = SemanticCache.make_key("same query", ["chunk-1"], "model-x", tenant="tenant_b")
        assert k1 != k2, "tenants with identical queries must not share a cache key"

    def test_same_tenant_same_key_stable(self):
        k1 = SemanticCache.make_key("q", ["c"], "m", tenant="t1")
        k2 = SemanticCache.make_key("q", ["c"], "m", tenant="t1")
        assert k1 == k2

    def test_tenant_isolation_in_get_set(self):
        cache = SemanticCache(ttl_seconds=60)
        k_a = SemanticCache.make_key("q", ["c"], "m", tenant="alice")
        k_b = SemanticCache.make_key("q", ["c"], "m", tenant="bob")
        cache.set(k_a, {"answer": "alice's answer"})
        # bob must not receive alice's cached answer
        assert cache.get(k_b) is None
        assert cache.get(k_a) == {"answer": "alice's answer"}

    def test_empty_tenant_differs_from_named_tenant(self):
        k_named = SemanticCache.make_key("q", ["c"], "m", tenant="tenant_x")
        k_empty = SemanticCache.make_key("q", ["c"], "m", tenant="")
        assert k_named != k_empty
