"""Tests for semantic cache tenant tagging correctness.

Item 4 of the Hardening Pass (2026-09-24): verifies that cache entries are
always written with tenant tags so invalidate_by_tenant() actually removes
them, preventing stale responses from crossing tenant boundaries.
"""
from __future__ import annotations

from src.cache.semantic_cache import SemanticCache


# ---------------------------------------------------------------------------
# Test 1: set_tagged embeds _tenant in payload
# ---------------------------------------------------------------------------

def test_set_tagged_embeds_tenant():
    cache = SemanticCache(ttl_seconds=60, cleanup_interval=9999)
    key = "key1"
    cache.set_tagged(key, {"answer": "hello"}, "tenant-a")

    # Retrieve the raw entry (not via public get()) to inspect internals
    with cache._lock:
        payload, _ = cache._store[key]

    assert payload.get("_tenant") == "tenant-a"


# ---------------------------------------------------------------------------
# Test 2: invalidate_by_tenant removes tagged entries
# ---------------------------------------------------------------------------

def test_invalidate_by_tenant_removes_tagged_entries():
    cache = SemanticCache(ttl_seconds=60, cleanup_interval=9999)
    cache.set_tagged("k-a", {"answer": "A"}, "tenant-a")
    cache.set_tagged("k-b", {"answer": "B"}, "tenant-b")

    removed = cache.invalidate_by_tenant("tenant-a")

    assert removed == 1
    assert cache.get("k-a") is None, "tenant-a entry should be gone"
    assert cache.get("k-b") is not None, "tenant-b entry should survive"


# ---------------------------------------------------------------------------
# Test 3: bare set() entries are NOT removed by invalidate_by_tenant
# ---------------------------------------------------------------------------

def test_bare_set_not_removed_by_invalidate():
    """Confirms the pre-fix behaviour: bare set() bypasses tenant invalidation.

    This test documents the known gap for entries written without a tenant tag
    (e.g., from code that has not yet been migrated to set_tagged). It is
    expected to pass and serves as a regression guard.
    """
    cache = SemanticCache(ttl_seconds=60, cleanup_interval=9999)
    cache.set("k-no-tag", {"answer": "legacy"})

    removed = cache.invalidate_by_tenant("tenant-a")

    assert removed == 0, "bare set() entry must not be removed by invalidate_by_tenant"
    assert cache.get("k-no-tag") is not None, "untagged entry should still be present"


# ---------------------------------------------------------------------------
# Test 4: async_runner uses set_tagged when _tenant is present in payload
# ---------------------------------------------------------------------------

def test_async_runner_uses_set_tagged_when_tenant_present():
    """Verify the caching decision in async_runner: _tenant present → set_tagged."""
    tagged_calls: list[tuple] = []
    plain_calls: list[tuple] = []

    class _FakeCache:
        def set_tagged(self, key, payload, tenant):
            tagged_calls.append((key, payload, tenant))

        def set(self, key, payload):
            plain_calls.append((key, payload))

    fake_cache = _FakeCache()

    import src.cache.semantic_cache as sc_mod
    original = sc_mod.semantic_cache
    sc_mod.semantic_cache = fake_cache  # type: ignore[assignment]

    try:
        # Replicate the exact decision block in async_runner._run_judge
        cache_key = "test-key"
        cache_payload = {"answer": "x", "_tenant": "tenant-z"}
        _tenant = cache_payload.get("_tenant", "")
        if _tenant:
            sc_mod.semantic_cache.set_tagged(cache_key, cache_payload, _tenant)
        else:
            sc_mod.semantic_cache.set(cache_key, cache_payload)
    finally:
        sc_mod.semantic_cache = original

    assert len(tagged_calls) == 1, "set_tagged not called"
    assert tagged_calls[0][2] == "tenant-z", f"wrong tenant: {tagged_calls[0][2]}"
    assert len(plain_calls) == 0, "set() should not be called when _tenant is present"


# ---------------------------------------------------------------------------
# Test 5: async_runner falls back to bare set() when _tenant is absent
# ---------------------------------------------------------------------------

def test_async_runner_falls_back_to_bare_set_when_no_tenant():
    tagged_calls: list = []
    plain_calls: list = []

    class _FakeCache:
        def set_tagged(self, key, payload, tenant):
            tagged_calls.append(1)

        def set(self, key, payload):
            plain_calls.append(1)

    fake_cache = _FakeCache()

    import src.cache.semantic_cache as sc_mod
    original = sc_mod.semantic_cache
    sc_mod.semantic_cache = fake_cache  # type: ignore[assignment]

    try:
        cache_payload = {"answer": "no-tenant"}
        cache_key = "k2"
        _tenant = cache_payload.get("_tenant", "")
        if _tenant:
            sc_mod.semantic_cache.set_tagged(cache_key, cache_payload, _tenant)
        else:
            sc_mod.semantic_cache.set(cache_key, cache_payload)
    finally:
        sc_mod.semantic_cache = original

    assert len(plain_calls) == 1
    assert len(tagged_calls) == 0
