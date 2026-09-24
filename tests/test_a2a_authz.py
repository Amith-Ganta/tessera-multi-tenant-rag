"""Tests for A2A service-to-service authorization boundary.

Item 1 of the Hardening Pass (2026-09-24): verifies that Tessera's Drafter and
Judge agents enforce service authentication when TESSERA_A2A_SERVICE_TOKEN is
set, and that the supervisor injects the correct header on outbound calls.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_jsonrpc_body(metadata: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "test-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "{}"}],
                "metadata": metadata or {},
            }
        },
    }


def _make_agent_app(authenticate: bool = True):
    """Build a lightweight A2A app with a trivial handler (no real RAG)."""
    from src.agents.a2a_protocol import make_a2a_app

    def _handler(params: dict) -> dict:
        return {"tenant": params.get("tenant_slug", "default"), "ok": True}

    return make_a2a_app(
        name="test-agent",
        description="test",
        url="http://localhost:9999",
        skill_id="test_skill",
        skill_name="test_skill",
        skill_description="test",
        handler=_handler,
        authenticate=authenticate,
    )


# ---------------------------------------------------------------------------
# Test 1: valid authorized tenant — supervisor sends correct service token
# ---------------------------------------------------------------------------

def test_valid_service_token_accepted(monkeypatch):
    """Agent accepts request when the header carries the correct service token."""
    monkeypatch.setenv("TESSERA_A2A_SERVICE_TOKEN", "secret-token-abc")
    app = _make_agent_app()
    client = TestClient(app, raise_server_exceptions=True)

    body = _minimal_jsonrpc_body({"tenant_slug": "user-1", "question": "hello"})
    resp = client.post(
        "/",
        json=body,
        headers={"X-Tessera-Service-Token": "secret-token-abc"},
    )
    assert resp.status_code == 200
    rpc = resp.json()
    assert rpc.get("result") is not None, f"expected result, got: {rpc}"


# ---------------------------------------------------------------------------
# Test 2: wrong service token is rejected
# ---------------------------------------------------------------------------

def test_wrong_service_token_rejected(monkeypatch):
    """Agent rejects request carrying a token that does not match the secret."""
    monkeypatch.setenv("TESSERA_A2A_SERVICE_TOKEN", "correct-secret")
    app = _make_agent_app()
    client = TestClient(app, raise_server_exceptions=True)

    body = _minimal_jsonrpc_body({"tenant_slug": "user-1", "question": "hello"})
    resp = client.post(
        "/",
        json=body,
        headers={"X-Tessera-Service-Token": "wrong-secret"},
    )
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 3: missing service token header is rejected
# ---------------------------------------------------------------------------

def test_missing_service_token_rejected(monkeypatch):
    """Agent rejects request that carries no service token when one is required."""
    monkeypatch.setenv("TESSERA_A2A_SERVICE_TOKEN", "must-supply-this")
    app = _make_agent_app()
    client = TestClient(app, raise_server_exceptions=True)

    body = _minimal_jsonrpc_body({"tenant_slug": "user-2", "question": "hi"})
    resp = client.post("/", json=body)  # no X-Tessera-Service-Token header
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 4: no token configured → open mode (dev/test) — still serves the request
# ---------------------------------------------------------------------------

def test_open_mode_when_token_not_configured(monkeypatch):
    """When TESSERA_A2A_SERVICE_TOKEN is absent the agent serves any caller (dev mode)."""
    monkeypatch.delenv("TESSERA_A2A_SERVICE_TOKEN", raising=False)
    app = _make_agent_app()
    client = TestClient(app, raise_server_exceptions=True)

    body = _minimal_jsonrpc_body({"tenant_slug": "user-3", "question": "hi"})
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    rpc = resp.json()
    assert rpc.get("result") is not None


# ---------------------------------------------------------------------------
# Test 5: supervisor injects service token into outbound A2A HTTP calls
# ---------------------------------------------------------------------------

def test_supervisor_injects_service_token(monkeypatch):
    """A2AJsonRpcClient.send_message injects X-Tessera-Service-Token when env var is set."""
    monkeypatch.setenv("TESSERA_A2A_SERVICE_TOKEN", "supervisor-secret")

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": "x",
                "result": {
                    "artifacts": [
                        {
                            "parts": [
                                {"kind": "data", "data": {"draft": "ok", "tenant": "user-1"}}
                            ]
                        }
                    ]
                },
            }

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers or {}
        return _FakeResponse()

    from src.orchestrator.a2a_supervisor import A2AJsonRpcClient

    with patch("src.orchestrator.a2a_supervisor.requests.post", side_effect=_fake_post):
        client = A2AJsonRpcClient("http://localhost:8001")
        client.send_message({"question": "q", "tenant_slug": "user-1"})

    assert "X-Tessera-Service-Token" in captured["headers"], (
        f"supervisor did not inject service token header; got headers: {captured['headers']}"
    )
    assert captured["headers"]["X-Tessera-Service-Token"] == "supervisor-secret"


# ---------------------------------------------------------------------------
# Test 6: supervisor omits service token header when env var is not set
# ---------------------------------------------------------------------------

def test_supervisor_omits_token_when_not_configured(monkeypatch):
    """A2AJsonRpcClient does not add the header when TESSERA_A2A_SERVICE_TOKEN is absent."""
    monkeypatch.delenv("TESSERA_A2A_SERVICE_TOKEN", raising=False)

    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jsonrpc": "2.0", "id": "x", "result": {"artifacts": []}}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers or {}
        return _FakeResponse()

    from src.orchestrator.a2a_supervisor import A2AJsonRpcClient

    with patch("src.orchestrator.a2a_supervisor.requests.post", side_effect=_fake_post):
        client = A2AJsonRpcClient("http://localhost:8001")
        client.send_message({"question": "q", "tenant_slug": "user-1"})

    assert "X-Tessera-Service-Token" not in captured["headers"], (
        "supervisor should not inject token header when none is configured"
    )
