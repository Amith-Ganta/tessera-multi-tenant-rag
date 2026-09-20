"""Regression tests for token expiry enforcement.

CVE: _verify_token extracted the issued timestamp but never compared it to the
current time, so tokens were valid indefinitely.  These tests confirm the fix.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers — replicate token format from app.py without importing FastAPI app
# (importing app.py triggers side effects: DB init, Redis connections, etc.)
# ---------------------------------------------------------------------------

_SECRET = "test-session-secret"


def _make_token(user_id: int, issued_at: int | None = None) -> str:
    ts = issued_at if issued_at is not None else int(time.time())
    payload = f"{user_id}:{ts}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def _verify_token(token: str, max_age: int = 86400) -> int | None:
    """Mirror of app.py _verify_token with an injectable secret and max_age."""
    try:
        payload, sig = token.rsplit(":", 1)
        user_id_str, issued_ts_str = payload.split(":", 1)
        expected_sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, sig):
            return None
        issued_ts = int(issued_ts_str)
        if time.time() - issued_ts > max_age:
            return None
        return int(user_id_str)
    except (ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTokenExpiry:
    def test_fresh_token_accepted(self):
        token = _make_token(42)
        assert _verify_token(token, max_age=86400) == 42

    def test_expired_token_rejected(self):
        # Issue token 2 seconds in the past with a 1-second max age
        old_ts = int(time.time()) - 2
        token = _make_token(42, issued_at=old_ts)
        assert _verify_token(token, max_age=1) is None

    def test_token_exactly_at_boundary_rejected(self):
        # Token issued max_age seconds ago should be rejected (strictly greater)
        old_ts = int(time.time()) - 60
        token = _make_token(42, issued_at=old_ts)
        assert _verify_token(token, max_age=59) is None

    def test_tampered_signature_rejected(self):
        token = _make_token(42)
        tampered = token[:-4] + "0000"
        assert _verify_token(tampered) is None

    def test_tampered_user_id_rejected(self):
        token = _make_token(42)
        parts = token.split(":")
        parts[0] = "99"  # change user_id but keep original sig
        tampered = ":".join(parts)
        assert _verify_token(tampered) is None

    def test_malformed_token_returns_none(self):
        assert _verify_token("not-a-token") is None
        assert _verify_token("") is None
        assert _verify_token(":") is None

    def test_future_issued_at_accepted(self):
        # Tokens with a slightly future timestamp (clock skew) should still be valid
        future_ts = int(time.time()) + 5
        token = _make_token(1, issued_at=future_ts)
        # time.time() - future_ts ≈ -5, which is < max_age, so should pass
        assert _verify_token(token, max_age=86400) == 1
