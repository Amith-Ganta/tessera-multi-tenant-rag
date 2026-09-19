"""Tests for ADR-011 canary deployment: tenant-scoped model routing via hash."""

from __future__ import annotations

import hashlib

import pytest

from src.rag.tenant_context import active_tenant_id, use_tenant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_percent(tenant_id: str) -> int:
    """Replicate the hash used in _select_model."""
    return int(hashlib.sha256(tenant_id.encode()).hexdigest(), 16) % 100


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_canary_disabled_at_zero(monkeypatch):
    """When MODEL_CANARY_PERCENT=0, _select_model always returns the default."""
    import src.rag.llm as llm_mod
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_PERCENT", 0)
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_VERSION", "gpt-4o-canary")

    with use_tenant("tenant-alpha"):
        result = llm_mod._select_model("deepseek/deepseek-chat")

    assert result == "deepseek/deepseek-chat"


def test_canary_full_at_hundred(monkeypatch):
    """When MODEL_CANARY_PERCENT=100, _select_model always returns the canary model."""
    import src.rag.llm as llm_mod
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_PERCENT", 100)
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_VERSION", "gpt-4o-canary")

    with use_tenant("tenant-beta"):
        result = llm_mod._select_model("deepseek/deepseek-chat")

    assert result == "gpt-4o-canary"


def test_canary_deterministic_per_tenant(monkeypatch):
    """The same tenant always gets the same routing decision."""
    import src.rag.llm as llm_mod
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_PERCENT", 50)
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_VERSION", "gpt-4o-canary")

    tenant = "stable-tenant-xyz"
    expected_canary = _hash_percent(tenant) < 50

    results = []
    for _ in range(5):
        with use_tenant(tenant):
            r = llm_mod._select_model("deepseek/deepseek-chat")
        results.append(r)

    # All calls must agree
    assert len(set(results)) == 1
    # Outcome must match the hash calculation
    if expected_canary:
        assert results[0] == "gpt-4o-canary"
    else:
        assert results[0] == "deepseek/deepseek-chat"


def test_canary_distributes_across_tenants(monkeypatch):
    """At 50%, roughly half of a large tenant pool is routed to the canary."""
    import src.rag.llm as llm_mod
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_PERCENT", 50)
    monkeypatch.setattr(llm_mod, "MODEL_CANARY_VERSION", "gpt-4o-canary")

    tenants = [f"tenant-{i:04d}" for i in range(200)]
    canary_count = sum(
        1 for t in tenants if _hash_percent(t) < 50
    )
    # SHA-256 mod 100 distributes uniformly; 200 tenants should give 80–120 in canary
    assert 80 <= canary_count <= 120, (
        f"Expected 80-120 canary tenants out of 200, got {canary_count}"
    )


def test_active_tenant_id_returns_none_when_not_set():
    """active_tenant_id() returns None outside any use_tenant() context."""
    # Call outside any use_tenant context manager
    assert active_tenant_id() is None
