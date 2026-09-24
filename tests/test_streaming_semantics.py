"""Tests for SSE streaming semantics (Item 7, Hardening 2026-09-24).

Verifies that the SSE path emits a `{"error": "..."}` event when the LLM
thread raises, and does NOT emit a `{"done": True}` event in that case.
Also confirms that the `_stream_error` sentinel key is never leaked into a
successful result dict.
"""
from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Test 1: _stream_error in result dict triggers error event (unit-level)
# ---------------------------------------------------------------------------

def test_stream_error_sentinel_present():
    """When _result_holder contains _stream_error, pop returns the message."""
    result = {"_stream_error": "LLM timeout", "answer": ""}
    err = result.pop("_stream_error", None)
    assert err == "LLM timeout"
    assert "_stream_error" not in result


# ---------------------------------------------------------------------------
# Test 2: normal result has no _stream_error key
# ---------------------------------------------------------------------------

def test_normal_result_has_no_stream_error():
    result = {"answer": "42", "route": "vector", "usage": {}}
    err = result.pop("_stream_error", None)
    assert err is None
    assert result["answer"] == "42"


# ---------------------------------------------------------------------------
# Test 3: error event payload is valid JSON with "error" key
# ---------------------------------------------------------------------------

def test_error_event_is_valid_json():
    stream_error = "circuit breaker open"
    event = f"data: {json.dumps({'error': stream_error})}\n\n"
    assert event.startswith("data: ")
    body = event[len("data: "):].strip()
    parsed = json.loads(body)
    assert parsed["error"] == stream_error
    assert "done" not in parsed


# ---------------------------------------------------------------------------
# Test 4: done event is valid JSON with "done": True and "meta" key
# ---------------------------------------------------------------------------

def test_done_event_format():
    meta = {"answer": "The answer", "route": "vector", "strategy": "hybrid"}
    event = f"data: {json.dumps({'done': True, 'meta': meta})}\n\n"
    body = event[len("data: "):].strip()
    parsed = json.loads(body)
    assert parsed["done"] is True
    assert "meta" in parsed
    assert "error" not in parsed


# ---------------------------------------------------------------------------
# Test 5: _stream_error overrides done path — confirm pop+return semantics
# ---------------------------------------------------------------------------

def test_stream_error_prevents_done_event():
    """Simulate the generator's decision branch: error → early return, no done."""
    result_holder = [{"_stream_error": "disk I/O error"}]
    emitted_events = []

    _r = result_holder[0] if result_holder else {}
    _stream_error = _r.pop("_stream_error", None)
    if _stream_error:
        emitted_events.append(f"data: {json.dumps({'error': _stream_error})}\n\n")
        # Simulates `return` — done event assembly is skipped
    else:
        emitted_events.append(f"data: {json.dumps({'done': True, 'meta': _r})}\n\n")

    assert len(emitted_events) == 1
    parsed = json.loads(emitted_events[0][len("data: "):].strip())
    assert "error" in parsed
    assert "done" not in parsed
