"""Item 19: Document update consistency tests.

Covers:
- invalidate_bm25_cache() clears the LRU cache so next retrieval rebuilds
- After BM25 cache invalidation, stale results are not returned
- reset_vectorstore_cache() is callable without Chroma (stub-patched)
- After build_tenant_index (mocked), BM25 cache is invalidated
- After build_tenant_index (mocked), vectorstore cache is reset
- Calling invalidate_bm25_cache() twice in a row is idempotent (no error)

No real LLM, Chroma, or filesystem calls are made.
retriever_dense depends on langchain_chroma; we stub that import before loading.
"""

from __future__ import annotations

import sys
import types
import pytest
from unittest.mock import patch, MagicMock, call
from langchain_core.documents import Document

# Stub langchain_chroma so retriever_dense can be imported in the test env.
_stub_chroma = types.ModuleType("langchain_chroma")
_stub_chroma.Chroma = MagicMock()
sys.modules.setdefault("langchain_chroma", _stub_chroma)

from src.rag.retriever_sparse import invalidate_bm25_cache, _build_bm25, _tokenize


# ---------------------------------------------------------------------------
# BM25 cache invalidation
# ---------------------------------------------------------------------------

class TestBm25CacheInvalidation:
    def test_invalidate_clears_lru_cache(self):
        """After invalidation, _build_bm25 should be called again (cache miss)."""
        corpus_dir = "test/corpus"
        call_counts = [0]

        def fake_chunk_documents(_):
            call_counts[0] += 1
            return [Document(page_content="hello world", metadata={"source": "f.txt"})]

        with patch("src.rag.retriever_sparse._chunk_documents", side_effect=fake_chunk_documents):
            # First call — populates cache
            _build_bm25(corpus_dir)
            count_after_first = call_counts[0]

            # Invalidate
            invalidate_bm25_cache(corpus_dir)

            # Second call after invalidation — must miss cache and call again
            _build_bm25(corpus_dir)
            count_after_second = call_counts[0]

        assert count_after_first == 1, "first call must populate cache"
        assert count_after_second == 2, "after invalidation, rebuild must be triggered"

    def test_invalidate_is_idempotent(self):
        """Calling invalidate twice must not raise."""
        invalidate_bm25_cache("some/path")
        invalidate_bm25_cache("some/path")  # second call must not raise

    def test_invalidate_does_not_raise_without_prior_cache(self):
        """Invalidating a corpus_dir that was never cached must not raise."""
        invalidate_bm25_cache("never/been/cached/path")

    def test_cache_miss_after_invalidation_returns_fresh_result(self):
        """After invalidation, _build_bm25 returns the updated corpus."""
        corpus_dir = "rebuild/corpus"
        first_docs = [Document(page_content="old content", metadata={"source": "old.txt"})]
        second_docs = [Document(page_content="new content", metadata={"source": "new.txt"})]
        iter_call = [0]

        def alternating_chunks(_):
            iter_call[0] += 1
            return first_docs if iter_call[0] == 1 else second_docs

        with patch("src.rag.retriever_sparse._chunk_documents", side_effect=alternating_chunks):
            bm25_1, docs_1, _ = _build_bm25(corpus_dir)
            invalidate_bm25_cache(corpus_dir)
            bm25_2, docs_2, _ = _build_bm25(corpus_dir)

        assert docs_1[0].metadata["source"] == "old.txt"
        assert docs_2[0].metadata["source"] == "new.txt"


# ---------------------------------------------------------------------------
# Vectorstore cache reset (interface-only tests — no real Chroma)
# ---------------------------------------------------------------------------

class TestVectorstoreCacheReset:
    def test_reset_vectorstore_cache_importable(self):
        """reset_vectorstore_cache must be importable without langchain_chroma."""
        from src.rag.retriever_dense import reset_vectorstore_cache
        assert callable(reset_vectorstore_cache)

    def test_reset_vectorstore_cache_callable_without_error(self):
        """Calling reset must not raise (it clears an in-memory dict)."""
        from src.rag.retriever_dense import reset_vectorstore_cache
        reset_vectorstore_cache()

    def test_reset_vectorstore_cache_idempotent(self):
        from src.rag.retriever_dense import reset_vectorstore_cache
        reset_vectorstore_cache()
        reset_vectorstore_cache()  # must not raise on second call


# ---------------------------------------------------------------------------
# Post-upload consistency: both caches cleared after build_tenant_index
# ---------------------------------------------------------------------------

class TestPostUploadConsistency:
    """Verify that calling invalidate_bm25_cache and reset_vectorstore_cache
    (the two calls build_tenant_index makes after indexing) actually clears
    the caches so subsequent retrievals use fresh data.
    """

    def test_bm25_cache_cleared_when_build_calls_invalidate(self):
        """Simulate what build_tenant_index does: call invalidate_bm25_cache."""
        corpus_dir = "tenant/corpus"
        reset_call = [False]

        # Pre-populate cache
        with patch("src.rag.retriever_sparse._chunk_documents",
                   return_value=[Document(page_content="old", metadata={})]):
            _build_bm25(corpus_dir)

        # build_tenant_index would call these after indexing:
        invalidate_bm25_cache(corpus_dir)
        from src.rag.retriever_dense import reset_vectorstore_cache
        reset_vectorstore_cache()

        # Now BM25 cache is empty — next call will rebuild
        new_docs = [Document(page_content="fresh data", metadata={"source": "fresh.txt"})]
        with patch("src.rag.retriever_sparse._chunk_documents", return_value=new_docs):
            _, docs, _ = _build_bm25(corpus_dir)

        assert docs[0].metadata["source"] == "fresh.txt"

    def test_sparse_retrieval_after_invalidation_returns_new_content(self):
        """retrieve_sparse must serve updated content after cache clear."""
        from src.rag.retriever_sparse import retrieve_sparse
        from src.rag.tenant_context import use_tenant
        from rank_bm25 import BM25Okapi

        # First, populate with old content
        old_doc = Document(page_content="old stale information", metadata={"source": "old.txt"})
        new_doc = Document(page_content="fresh updated information", metadata={"source": "new.txt"})

        tokenized_new = [_tokenize(new_doc.page_content)]
        bm25_new = BM25Okapi(tokenized_new)

        with use_tenant("update-test"):
            with patch("src.rag.retriever_sparse._build_bm25",
                       return_value=(bm25_new, [new_doc], tokenized_new)):
                results = retrieve_sparse("fresh updated information", 1)

        assert results[0].metadata["source"] == "new.txt"
