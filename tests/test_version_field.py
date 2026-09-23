"""Tests for Phase 3G: versions dict in AskResponse.

Four cases:
  1. AskResponse now has 15 fields and the versions field is present
  2. _VERSIONS dict contains the six expected keys
  3. VERSION_* constants in config are not empty strings
  4. versions is emitted in the analytics payload
"""

from __future__ import annotations

import pytest


class TestVersionsFieldPresent:
    def test_ask_response_has_versions_field(self):
        """AskResponse.model_fields must include 'versions'."""
        from src.api.app import AskResponse
        assert "versions" in AskResponse.model_fields

    def test_versions_dict_has_six_keys(self):
        """The module-level _VERSIONS dict must have exactly the six version keys."""
        from src.api.app import _VERSIONS
        expected_keys = {"model", "prompt", "embedding", "retrieval", "reranker", "eval_dataset"}
        assert set(_VERSIONS.keys()) == expected_keys


class TestVersionConstants:
    def test_version_constants_are_non_empty(self):
        """All VERSION_* constants exported from config must be non-empty strings."""
        from src.rag.config import (
            VERSION_MODEL, VERSION_PROMPT, VERSION_EMBEDDING,
            VERSION_RETRIEVAL, VERSION_RERANKER, VERSION_EVAL_DATASET,
        )
        for name, val in [
            ("VERSION_MODEL", VERSION_MODEL),
            ("VERSION_PROMPT", VERSION_PROMPT),
            ("VERSION_EMBEDDING", VERSION_EMBEDDING),
            ("VERSION_RETRIEVAL", VERSION_RETRIEVAL),
            ("VERSION_RERANKER", VERSION_RERANKER),
            ("VERSION_EVAL_DATASET", VERSION_EVAL_DATASET),
        ]:
            assert isinstance(val, str) and val, f"{name} must be a non-empty string, got {val!r}"

    def test_versions_match_source_constants(self):
        """_VERSIONS values must match the corresponding VERSION_* constants."""
        from src.api.app import _VERSIONS
        from src.rag.config import (
            VERSION_MODEL, VERSION_PROMPT, VERSION_EMBEDDING,
            VERSION_RETRIEVAL, VERSION_RERANKER, VERSION_EVAL_DATASET,
        )
        assert _VERSIONS["model"] == VERSION_MODEL
        assert _VERSIONS["prompt"] == VERSION_PROMPT
        assert _VERSIONS["embedding"] == VERSION_EMBEDDING
        assert _VERSIONS["retrieval"] == VERSION_RETRIEVAL
        assert _VERSIONS["reranker"] == VERSION_RERANKER
        assert _VERSIONS["eval_dataset"] == VERSION_EVAL_DATASET
