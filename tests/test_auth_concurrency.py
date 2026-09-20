"""Tests for async PBKDF2 wrappers and auth concurrency behaviour."""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from src.security.async_crypto import (
    _pbkdf2_pool,
    hash_password_async,
    verify_password_async,
)
from src.auth.auth import hash_password as _hash_sync, verify_password as _verify_sync


# ---------------------------------------------------------------------------
# 1. Thread pool configuration
# ---------------------------------------------------------------------------

def test_bcrypt_runs_in_thread_pool():
    """Pool is configured with the expected parallelism ceiling."""
    assert _pbkdf2_pool._max_workers == 4


# ---------------------------------------------------------------------------
# 2. Hash / verify round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hash_and_verify_roundtrip():
    password = "correct-horse-battery-staple"
    wrong = "wrong-password"

    hashed = await hash_password_async(password)
    assert isinstance(hashed, str)
    assert len(hashed) > 10

    assert await verify_password_async(password, hashed) is True
    assert await verify_password_async(wrong, hashed) is False


# ---------------------------------------------------------------------------
# 3. Concurrent verify calls don't serialise on the event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_logins_do_not_serialize_on_event_loop():
    """10 concurrent verifications complete faster than 10× the single call time."""
    password = "parallel-test-password"
    hashed = _hash_sync(password)  # pre-compute once — not what we're measuring

    # Measure a single call baseline.
    t0 = time.perf_counter()
    await verify_password_async(password, hashed)
    single_call_s = time.perf_counter() - t0

    # Run 10 concurrent calls and measure wall time.
    t1 = time.perf_counter()
    results = await asyncio.gather(
        *[verify_password_async(password, hashed) for _ in range(10)]
    )
    wall_s = time.perf_counter() - t1

    assert all(results), "all 10 verifications should return True"
    # If calls were fully serialised, wall_s ≈ 10 × single_call_s.
    # With 4 threads and 10 jobs, the expected wall time is ~3 × single_call_s
    # (three batches of 4). Accept anything under 8× to give plenty of tolerance
    # for slow CI boxes while still catching true serialisation.
    tolerance = 8
    assert wall_s < tolerance * single_call_s, (
        f"Looks serialised: wall={wall_s:.2f}s, single={single_call_s:.2f}s, "
        f"ratio={wall_s / single_call_s:.1f}x (limit={tolerance}x)"
    )


# ---------------------------------------------------------------------------
# 4. End-to-end: signup → login round-trip via HTTP
# ---------------------------------------------------------------------------

def test_signup_and_login_still_work():
    """Signup a fresh user then login — async handlers must preserve semantics."""
    from src.api.app import app

    client = TestClient(app)
    unique_email = f"concurrency-test-{uuid.uuid4().hex[:8]}@tessera.local"
    password = "testpass123"

    # Signup
    r = client.post("/auth/signup", json={"email": unique_email, "password": password})
    assert r.status_code == 200, f"signup failed: {r.text}"

    # Correct password → 200
    r = client.post("/auth/login", json={"email": unique_email, "password": password})
    assert r.status_code == 200, f"login failed: {r.text}"
    data = r.json()
    assert "token" in data
    assert data["email"] == unique_email

    # Wrong password → 401
    r = client.post("/auth/login", json={"email": unique_email, "password": "wrongpass"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
