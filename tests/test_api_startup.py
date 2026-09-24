"""Regression test: app imports cleanly without env vars and /health returns 200.

This test guards against module-level code that raises at import time when
secrets are absent (e.g. a module-level `os.environ["DEEPSEEK_API_KEY"]` that
throws KeyError).  It also guards against the app failing to start for any
reason unrelated to an active LLM provider.

The test must be run via `uv run pytest` (or the project venv's pytest) because
the project dependencies are installed there, not in the system Python.
"""

from __future__ import annotations

import importlib
import os
import sys


def test_app_imports_without_secrets(monkeypatch):
    """App module must import without any API keys or secrets set."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("TESSERA_ENV", "dev")

    # Remove any cached import so monkeypatched env takes effect.
    for mod_name in list(sys.modules):
        if mod_name == "src.api.app" or mod_name.startswith("src.api.app."):
            del sys.modules[mod_name]

    # This must not raise.
    from src.api.app import app  # noqa: F401

    assert app is not None, "app object must be truthy after import"


def test_health_endpoint_returns_200():
    """GET /health must return HTTP 200 within 5 seconds using TestClient."""
    from fastapi.testclient import TestClient
    from src.api.app import app

    with TestClient(app) as client:
        response = client.get("/health", timeout=5)

    assert response.status_code == 200, (
        f"/health returned {response.status_code}: {response.text}"
    )
    assert response.json() == {"status": "ok"}, (
        f"unexpected /health body: {response.json()}"
    )
