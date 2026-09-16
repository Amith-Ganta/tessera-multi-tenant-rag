"""Tests for Phase 4c: streaming responses via server-sent events."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.app import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_sse_events(raw: str) -> list[dict]:
    """Parse raw SSE text into a list of data dicts."""
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payload = line[len("data: "):]
            events.append(json.loads(payload))
    return events


def _signup_and_login(client: TestClient) -> str:
    email = "stream-test@tessera.local"
    password = "stream-test-pw"
    client.post("/auth/signup", json={"email": email, "password": password})
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStreamingEndpoint:
    def test_non_streaming_request_returns_json(self):
        """Without Accept: text/event-stream, /ask returns normal JSON."""
        with TestClient(app) as client:
            token = _signup_and_login(client)
            resp = client.post(
                "/ask",
                json={"question": "What is this knowledge base about?"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "route" in data

    def test_streaming_request_returns_sse_content_type(self):
        """Accept: text/event-stream triggers SSE media type."""
        with TestClient(app) as client:
            token = _signup_and_login(client)
            resp = client.post(
                "/ask",
                json={"question": "What is this knowledge base about?"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_streaming_response_has_token_events(self):
        """SSE stream contains at least one token event before the done event."""
        with TestClient(app) as client:
            token = _signup_and_login(client)
            resp = client.post(
                "/ask",
                json={"question": "What is this knowledge base about?"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            )
        events = _parse_sse_events(resp.text)
        token_events = [e for e in events if "token" in e]
        assert len(token_events) >= 1

    def test_streaming_response_ends_with_done_event(self):
        """Last SSE event is {done: true, meta: {...}}."""
        with TestClient(app) as client:
            token = _signup_and_login(client)
            resp = client.post(
                "/ask",
                json={"question": "Summarize the main topic."},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            )
        events = _parse_sse_events(resp.text)
        assert events, "No SSE events received"
        last = events[-1]
        assert last.get("done") is True
        assert "meta" in last

    def test_streaming_meta_has_all_14_fields(self):
        """The done event's meta contains all 14 AskResponse fields."""
        required_fields = {
            "answer", "route", "strategy", "model", "sources",
            "latency_ms", "tokens", "estimated_cost_usd", "tenant",
            "eval", "guard", "trace", "thread_id", "transcript",
        }
        with TestClient(app) as client:
            token = _signup_and_login(client)
            resp = client.post(
                "/ask",
                json={"question": "What problem does the described system solve?"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/event-stream",
                },
            )
        events = _parse_sse_events(resp.text)
        done_events = [e for e in events if e.get("done") is True]
        assert done_events, "No done event in SSE stream"
        meta = done_events[-1]["meta"]
        missing = required_fields - set(meta.keys())
        assert not missing, f"Missing fields in meta: {missing}"
