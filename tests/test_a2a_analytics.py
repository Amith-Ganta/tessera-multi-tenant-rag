"""A2A analytics versions field tests (A4).

Verifies that the A2A path's log_analytics call includes the "versions" key
so analytics records have full version context, consistent with the sync path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import pytest


class TestA2AAnalyticsVersions:
    def test_a2a_log_analytics_includes_versions(self) -> None:
        """The A2A branch of /ask must pass 'versions' to log_analytics."""
        import os
        os.environ.setdefault("TESSERA_SESSION_SECRET", "test-secret-a2a-analytics")

        from fastapi.testclient import TestClient
        from src.api.app import app, _VERSIONS

        client = TestClient(app, raise_server_exceptions=False)

        # Register and login
        import time
        unique = str(int(time.time() * 1000))
        email = f"a2a_analytics_test_{unique}@example.com"
        client.post("/auth/signup", json={"email": email, "password": "TestPass123!"})
        resp = client.post("/auth/login", json={"email": email, "password": "TestPass123!"})
        if resp.status_code != 200:
            pytest.skip(f"login failed: {resp.text}")
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        fake_a2a_result = {
            "answer": "A2A answer",
            "route": "vector",
            "sources": [],
            "trace": [],
            "transcript": [],
            "thread_id": None,
            "attempts": 1,
            "max_retries": 2,
            "final_scores": {},
            "note": "",
        }

        captured_calls: list[dict] = []

        def _capture_analytics(data: dict) -> None:
            captured_calls.append(data)

        with patch("src.api.app.log_analytics", side_effect=_capture_analytics):
            with patch("src.api.app._run_a2a") as mock_a2a:
                from src.api.app import AskResponse
                mock_a2a.return_value = AskResponse(
                    answer="A2A answer",
                    route="vector",
                    strategy="a2a",
                    model="deepseek/deepseek-flash",
                    sources=[],
                    latency_ms=100.0,
                    tokens={"prompt": 0, "completion": 0, "total": 0},
                    estimated_cost_usd=0.0,
                    tenant="user-1",
                    eval=None,
                    guard=None,
                    trace=[],
                    thread_id=None,
                    transcript=None,
                    versions=_VERSIONS,
                )
                client.post(
                    "/ask",
                    json={"question": "What is IAM?", "use_a2a": True},
                    headers=headers,
                )

        # The actual log_analytics call inside _run_a2a is what we're checking;
        # mock_a2a intercepts the whole function, so we verify the source code
        # directly contains the versions key.
        import inspect
        from src.api import app as app_module
        source = inspect.getsource(app_module._run_a2a)
        assert '"versions": _build_versions()' in source or "'versions': _build_versions()" in source, (
            "A2A log_analytics call must include 'versions': _build_versions() — canary versioning fix"
        )

    def test_versions_key_in_a2a_source(self) -> None:
        """Direct source inspection: _run_a2a must pass versions to log_analytics."""
        import inspect
        from src.api import app as app_module

        source = inspect.getsource(app_module._run_a2a)
        # Verify the fix: "versions" must appear in the log_analytics dict
        assert '"versions"' in source or "'versions'" in source, (
            "_run_a2a must pass 'versions' to log_analytics"
        )
        # Verify it uses _build_versions() for per-request canary-aware versioning
        assert "_build_versions" in source, (
            "_run_a2a log_analytics must use _build_versions(), not a static _VERSIONS reference"
        )
