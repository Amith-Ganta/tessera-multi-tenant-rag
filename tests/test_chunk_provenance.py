"""Item 15: Chunk provenance metadata tests.

Covers:
- Retrieved chunks carry 'source' metadata field
- 'source' field is non-empty string when document has a path
- Sparse retriever preserves source metadata from underlying Document
- Multiple chunks from the same document share identical source
- Chunk from different documents have different sources
- Provenance survives BM25 score-based ranking (top-k slice)
- Documents with no source metadata still produce a chunk (source defaults to "")

No real Chroma / OpenAI / ingest calls are made.
"""

from __future__ import annotations

import pytest
from langchain_core.documents import Document

from src.rag.retriever_sparse import _tokenize, retrieve_sparse
from src.rag.tenant_context import use_tenant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc(content: str, source: str, chunk_index: int = 0) -> Document:
    return Document(
        page_content=content,
        metadata={"source": source, "chunk_index": chunk_index},
    )


# ---------------------------------------------------------------------------
# Unit: Document metadata contract
# ---------------------------------------------------------------------------

class TestDocumentMetadataContract:
    def test_document_carries_source_field(self):
        doc = _make_doc("hello world", "/data/tenants/t1/corpus/doc1.txt")
        assert "source" in doc.metadata

    def test_source_is_non_empty_for_real_path(self):
        doc = _make_doc("content", "/data/tenants/t1/corpus/sample.md")
        assert doc.metadata["source"] != ""

    def test_source_defaults_empty_when_absent(self):
        doc = Document(page_content="no metadata here", metadata={})
        assert doc.metadata.get("source", "") == ""

    def test_chunk_index_field_present(self):
        doc = _make_doc("chunk zero text", "doc.txt", chunk_index=0)
        assert doc.metadata["chunk_index"] == 0

    def test_multiple_chunks_same_source(self):
        chunks = [
            _make_doc(f"part {i}", "doc.txt", chunk_index=i)
            for i in range(3)
        ]
        sources = [c.metadata["source"] for c in chunks]
        assert len(set(sources)) == 1, "all chunks from same doc must share one source"

    def test_chunks_from_different_docs_have_different_sources(self):
        chunk_a = _make_doc("text from doc a", "doc_a.txt")
        chunk_b = _make_doc("text from doc b", "doc_b.txt")
        assert chunk_a.metadata["source"] != chunk_b.metadata["source"]


# ---------------------------------------------------------------------------
# Unit: _tokenize helper
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic_split(self):
        assert _tokenize("hello world") == ["hello", "world"]

    def test_lowercases(self):
        assert _tokenize("Hello World") == ["hello", "world"]

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_filters_empty_tokens(self):
        result = _tokenize("  a  b  ")
        assert "" not in result
        assert "a" in result and "b" in result


# ---------------------------------------------------------------------------
# Integration: retrieve_sparse preserves provenance metadata
# ---------------------------------------------------------------------------

class TestRetrieveSparseProvenance:
    """Patch _build_bm25 so no real file I/O happens; verify metadata pass-through."""

    def _run(self, question: str, docs: list[Document], top_k: int = 3) -> list[Document]:
        from unittest.mock import patch
        from rank_bm25 import BM25Okapi

        tokenized = [_tokenize(d.page_content) for d in docs]
        bm25 = BM25Okapi(tokenized)

        with use_tenant("test-prov"):
            with patch("src.rag.retriever_sparse._build_bm25", return_value=(bm25, docs, tokenized)):
                return retrieve_sparse(question, top_k)

    def test_source_metadata_present_in_result(self):
        docs = [_make_doc("machine learning basics", "ml.txt", 0)]
        results = self._run("machine learning", docs)
        assert results, "expected at least one result"
        assert "source" in results[0].metadata

    def test_source_value_matches_original(self):
        docs = [_make_doc("deep learning neural nets", "dl.txt", 0)]
        results = self._run("neural networks", docs)
        assert results[0].metadata["source"] == "dl.txt"

    def test_top_k_limits_results(self):
        docs = [_make_doc(f"document about topic {i}", f"doc{i}.txt") for i in range(5)]
        results = self._run("topic", docs, top_k=3)
        assert len(results) <= 3

    def test_all_results_carry_source(self):
        docs = [_make_doc(f"sample text {i}", f"file{i}.txt") for i in range(4)]
        results = self._run("sample text", docs, top_k=4)
        for r in results:
            assert "source" in r.metadata

    def test_empty_corpus_returns_empty(self):
        from unittest.mock import patch
        with use_tenant("test-empty"):
            with patch("src.rag.retriever_sparse._build_bm25", return_value=(None, [], [])):
                results = retrieve_sparse("anything", 5)
        assert results == []

    def test_source_survives_ranking_reorder(self):
        """The BM25 ranker must not strip metadata when reordering results."""
        docs = [
            _make_doc("unrelated content", "low_score.txt"),
            _make_doc("highly relevant query match query match", "high_score.txt"),
        ]
        results = self._run("query match", docs, top_k=2)
        sources = [r.metadata["source"] for r in results]
        assert "high_score.txt" in sources
        assert "low_score.txt" in sources
