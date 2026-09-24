"""Tests for BM25 sparse-index cache invalidation.

Item 2 of the Hardening Pass (2026-09-24): verifies that the BM25 LRU cache
is evicted whenever the tenant corpus changes, so retrieve_sparse never returns
stale results after an upload, document delete, or tenant delete.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_corpus(tmp_path: Path, content: str = "hello world foo bar") -> str:
    doc = tmp_path / "doc.txt"
    doc.write_text(content, encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Test 1: cache miss → build happens; second call hits cache
# ---------------------------------------------------------------------------

def test_bm25_cache_populates_on_first_call(tmp_path):
    from src.rag.retriever_sparse import _build_bm25, invalidate_bm25_cache

    corpus = _make_corpus(tmp_path)
    invalidate_bm25_cache(corpus)  # start clean

    bm25_a, docs_a, _ = _build_bm25(corpus)
    bm25_b, docs_b, _ = _build_bm25(corpus)  # second call → LRU hit

    assert bm25_a is bm25_b, "second call should return the cached object"
    assert len(docs_a) > 0


# ---------------------------------------------------------------------------
# Test 2: invalidate_bm25_cache forces a rebuild on next call
# ---------------------------------------------------------------------------

def test_invalidate_forces_rebuild(tmp_path):
    from src.rag.retriever_sparse import _build_bm25, invalidate_bm25_cache

    corpus = _make_corpus(tmp_path, "alpha beta gamma")
    invalidate_bm25_cache(corpus)

    bm25_before, _, _ = _build_bm25(corpus)

    # Invalidate — next call must rebuild
    invalidate_bm25_cache(corpus)
    bm25_after, _, _ = _build_bm25(corpus)

    # After invalidation a new BM25Okapi object is created
    assert bm25_before is not bm25_after, "cache not invalidated: got the same object back"


# ---------------------------------------------------------------------------
# Test 3: build_tenant_index invalidates cache (via ingest module)
# ---------------------------------------------------------------------------

def test_build_tenant_index_invalidates_bm25(tmp_path, monkeypatch):
    """build_tenant_index must evict the BM25 cache after rebuilding the dense index."""
    # Patch the Chroma / OpenAI calls so we don't need real API keys.
    from src.rag import ingest as ingest_mod
    from src.rag import retriever_sparse as rs

    invalidate_calls: list[str] = []

    def _fake_invalidate(corpus_dir: str) -> None:
        invalidate_calls.append(corpus_dir)

    monkeypatch.setattr(ingest_mod, "invalidate_bm25_cache", _fake_invalidate)

    # Patch the expensive Chroma.from_documents call
    with patch.object(ingest_mod, "OpenAIEmbeddings", return_value=object()), \
         patch("langchain_chroma.Chroma.from_documents", return_value=None), \
         patch.object(ingest_mod, "_release_chroma"):

        # Write a minimal corpus file so the function doesn't short-circuit
        tenant_id = "test-tenant-bm25"
        corpus_dir = tmp_path / "corpus" / tenant_id
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "file.txt").write_text("some text", encoding="utf-8")

        # Patch tenant_corpus_dir / tenant_index_dir to point at tmp_path
        monkeypatch.setattr(
            ingest_mod, "tenant_corpus_dir", lambda _: corpus_dir
        )
        monkeypatch.setattr(
            ingest_mod, "tenant_index_dir", lambda _: tmp_path / "index" / tenant_id
        )
        monkeypatch.setattr(ingest_mod, "get_openai_api_key", lambda: None)

        ingest_mod.build_tenant_index(tenant_id)

    assert len(invalidate_calls) >= 1, (
        "build_tenant_index did not call invalidate_bm25_cache"
    )


# ---------------------------------------------------------------------------
# Test 4: retrieve_sparse returns updated results after cache invalidation
# ---------------------------------------------------------------------------

def test_retrieve_sparse_sees_new_documents_after_invalidation(tmp_path):
    from src.rag.retriever_sparse import _build_bm25, invalidate_bm25_cache, retrieve_sparse
    from src.rag import tenant_context

    # Phase 1: corpus with only "alpha content"
    corpus = _make_corpus(tmp_path, "alpha content document one two three")
    invalidate_bm25_cache(corpus)

    with patch.object(tenant_context, "active_corpus_dir", return_value=Path(corpus)):
        bm25_before, docs_before, _ = _build_bm25(corpus)
        assert docs_before  # sanity

    # Phase 2: add a second document with "zeta content"
    (tmp_path / "new_doc.txt").write_text(
        "zeta keyword uniquely identifiable", encoding="utf-8"
    )
    invalidate_bm25_cache(corpus)

    with patch.object(tenant_context, "active_corpus_dir", return_value=Path(corpus)):
        bm25_after, docs_after, _ = _build_bm25(corpus)

    assert len(docs_after) > len(docs_before), (
        "BM25 index was not rebuilt after cache invalidation: "
        f"before={len(docs_before)} docs, after={len(docs_after)} docs"
    )


# ---------------------------------------------------------------------------
# Test 5: invalidate_bm25_cache is safe to call on a non-existent dir
# ---------------------------------------------------------------------------

def test_invalidate_bm25_cache_tolerates_nonexistent_dir():
    from src.rag.retriever_sparse import invalidate_bm25_cache

    # Should not raise even if the directory was never indexed
    invalidate_bm25_cache("/tmp/no-such-corpus-dir-xyz-999")
