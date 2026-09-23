"""Concurrent-slot governance tests (A3).

Verifies that:
1. SSE requests now acquire_concurrent before streaming (OI-SSE-CONCURRENCY fix).
2. When the concurrent limit is exhausted, the SSE path returns 503.
3. The concurrent slot is released after the SSE stream ends.
4. Sync requests still respect the concurrent limit.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient


def _get_client():
    import os
    os.environ.setdefault("TESSERA_SESSION_SECRET", "test-secret-concurrent")
    from src.api.app import app
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers(client: TestClient) -> dict:
    """Register + login a test user, return Bearer headers."""
    import time
    unique = str(int(time.time() * 1000))
    email = f"concurrent_test_{unique}@example.com"
    client.post("/auth/signup", json={"email": email, "password": "TestPass123!"})
    resp = client.post("/auth/login", json={"email": email, "password": "TestPass123!"})
    if resp.status_code != 200:
        pytest.skip(f"login failed: {resp.text}")
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestSSEConcurrencyGuard:
    def test_sse_returns_503_when_concurrent_limit_exceeded(self) -> None:
        """SSE path must return 503 when acquire_concurrent returns False."""
        client = _get_client()
        headers = _auth_headers(client)

        with patch("src.api.app._tenant_governor") as mock_gov:
            mock_gov.check_token_budget.return_value = True
            mock_gov.acquire_concurrent.return_value = False  # limit hit

            resp = client.post(
                "/ask",
                # "adaptive" is a valid strategy — avoids the 400 from strategy validation
                json={"question": "What is RBAC?", "strategy": "adaptive"},
                headers={**headers, "accept": "text/event-stream"},
            )
        assert resp.status_code == 503
        assert "concurrent" in resp.json().get("error", "").lower()

    def test_sse_acquires_concurrent_slot(self) -> None:
        """acquire_concurrent must be called for SSE requests."""
        client = _get_client()
        headers = _auth_headers(client)

        with patch("src.api.app._tenant_governor") as mock_gov:
            mock_gov.check_token_budget.return_value = True
            mock_gov.acquire_concurrent.return_value = True
            mock_gov.release_concurrent.return_value = None

            with patch("src.api.app.run_strategy") as mock_rs:
                mock_rs.return_value = {
                    "answer": "ok",
                    "route": "vector",
                    "strategy": "adaptive",
                    "sources": [],
                    "usage": {"prompt": 1, "completion": 1, "total": 2},
                    "trace": [],
                }
                resp = client.post(
                    "/ask",
                    json={"question": "Hello?", "strategy": "adaptive"},
                    headers={**headers, "accept": "text/event-stream"},
                )

        # acquire_concurrent must have been called
        mock_gov.acquire_concurrent.assert_called()

    def test_sse_releases_concurrent_slot_after_stream(self) -> None:
        """release_concurrent must be called after the SSE stream completes."""
        client = _get_client()
        headers = _auth_headers(client)

        with patch("src.api.app._tenant_governor") as mock_gov:
            mock_gov.check_token_budget.return_value = True
            mock_gov.acquire_concurrent.return_value = True
            mock_gov.release_concurrent.return_value = None

            with patch("src.api.app.run_strategy") as mock_rs:
                mock_rs.return_value = {
                    "answer": "done",
                    "route": "vector",
                    "strategy": "adaptive",
                    "sources": [],
                    "usage": {"prompt": 1, "completion": 1, "total": 2},
                    "trace": [],
                }
                # Consume the full response so the generator's finally block runs
                resp = client.post(
                    "/ask",
                    json={"question": "Hello?", "strategy": "adaptive"},
                    headers={**headers, "accept": "text/event-stream"},
                )
                # Read all content to exhaust the stream and trigger finally
                _ = resp.content

        # release_concurrent must have been called at least once
        mock_gov.release_concurrent.assert_called()


class TestSyncConcurrencyGuard:
    def test_sync_returns_503_when_concurrent_limit_exceeded(self) -> None:
        """Sync /ask path must also return 503 when acquire_concurrent returns False."""
        client = _get_client()
        headers = _auth_headers(client)

        with patch("src.api.app._tenant_governor") as mock_gov:
            mock_gov.check_token_budget.return_value = True
            mock_gov.acquire_concurrent.return_value = False

            resp = client.post(
                "/ask",
                # "adaptive" is a valid strategy — avoids 400 from strategy validation
                json={"question": "What is RBAC?", "strategy": "adaptive"},
                headers=headers,
            )
        assert resp.status_code == 503
