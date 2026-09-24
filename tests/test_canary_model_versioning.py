"""Tests for canary model versioning in AskResponse (Item 8, Hardening 2026-09-24).

Verifies that _build_versions() in app.py returns the actually-served model name
so operators can observe canary traffic from the versions field in responses.
"""
from __future__ import annotations

from unittest.mock import patch


# ---------------------------------------------------------------------------
# Test 1: when canary is inactive, _build_versions returns default _VERSIONS
# ---------------------------------------------------------------------------

def test_build_versions_default_no_canary():
    from src.api.app import _VERSIONS, _build_versions

    with patch("src.rag.llm.select_model", return_value="deepseek/deepseek-flash"):
        result = _build_versions()

    assert result is _VERSIONS, "should return singleton when canary not active"


# ---------------------------------------------------------------------------
# Test 2: when canary is active, _build_versions returns new dict with canary model
# ---------------------------------------------------------------------------

def test_build_versions_canary_active():
    from src.api.app import _VERSIONS, _build_versions

    with patch("src.rag.llm.select_model", return_value="gpt-4o-2024-11-20"):
        result = _build_versions()

    assert result is not _VERSIONS, "should return new dict when canary is active"
    assert result["model"] == "gpt-4o-2024-11-20"
    # All other keys must be preserved unchanged
    for key in ("prompt", "embedding", "retrieval", "reranker", "eval_dataset"):
        assert result[key] == _VERSIONS[key], f"non-model key {key!r} must be unchanged"


# ---------------------------------------------------------------------------
# Test 3: canary result does not mutate the original _VERSIONS dict
# ---------------------------------------------------------------------------

def test_build_versions_does_not_mutate_base():
    from src.api.app import _VERSIONS, _build_versions

    original_model = _VERSIONS["model"]
    with patch("src.rag.llm.select_model", return_value="gpt-4o-2024-11-20"):
        _build_versions()

    assert _VERSIONS["model"] == original_model, "_VERSIONS must not be mutated"


# ---------------------------------------------------------------------------
# Test 4: select_model is called with CHAT_MODEL, not an arbitrary value
# ---------------------------------------------------------------------------

def test_build_versions_passes_chat_model():
    from src.api.app import _build_versions
    from src.api.app import CHAT_MODEL

    with patch("src.rag.llm.select_model", return_value=CHAT_MODEL) as mock_select:
        _build_versions()

    mock_select.assert_called_once_with(CHAT_MODEL)


# ---------------------------------------------------------------------------
# Test 5: select_model (public alias) delegates to _select_model
# ---------------------------------------------------------------------------

def test_select_model_public_alias_delegates():
    from src.rag.llm import select_model, _select_model

    assert select_model is _select_model, "select_model must be an alias for _select_model"
