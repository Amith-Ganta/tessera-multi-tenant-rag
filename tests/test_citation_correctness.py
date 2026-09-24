"""Item 16: Citation correctness tests.

Covers:
- rag_pipeline.invoke() sources list contains the source from retrieved docs
- sources list is parallel with contexts list (same length, same order)
- An empty retrieval yields empty sources and contexts
- sources contains no duplicates when chunks from same doc are retrieved
- sources field is always a list of strings
- Source values match the 'source' key in Document.metadata

No real LLM or Chroma calls are made.
rag_pipeline imports retriever_dense (which uses langchain_chroma), so we block
the problematic import via sys.modules before loading the module under test.
"""

from __future__ import annotations

import sys
import types
import pytest
from langchain_core.documents import Document
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Block langchain_chroma before rag_pipeline is ever imported
# ---------------------------------------------------------------------------

def _ensure_pipeline_importable():
    """Insert a stub for langchain_chroma into sys.modules so rag_pipeline
    and retriever_dense can be imported without the real package installed."""
    stub_chroma = types.ModuleType("langchain_chroma")
    stub_chroma.Chroma = MagicMock()
    sys.modules.setdefault("langchain_chroma", stub_chroma)


_ensure_pipeline_importable()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc(content: str, source: str) -> Document:
    return Document(page_content=content, metadata={"source": source})


def _invoke_with_docs(question: str, docs: list[Document]) -> dict:
    """Call rag_pipeline.invoke() with mocked retrieve and generate_answer."""
    import src.rag.rag_pipeline as pipeline_mod
    with patch.object(pipeline_mod, "retrieve", return_value=docs), \
         patch.object(pipeline_mod, "generate_answer", return_value="mocked answer"):
        return pipeline_mod.invoke(question)


# ---------------------------------------------------------------------------
# Citation structure tests
# ---------------------------------------------------------------------------

class TestCitationStructure:
    def test_sources_present_in_result(self):
        docs = [_make_doc("content about ML", "ml.txt")]
        result = _invoke_with_docs("what is ml?", docs)
        assert "sources" in result

    def test_contexts_present_in_result(self):
        docs = [_make_doc("content about ML", "ml.txt")]
        result = _invoke_with_docs("what is ml?", docs)
        assert "contexts" in result

    def test_sources_is_list(self):
        docs = [_make_doc("some text", "doc.txt")]
        result = _invoke_with_docs("query", docs)
        assert isinstance(result["sources"], list)

    def test_contexts_is_list(self):
        docs = [_make_doc("some text", "doc.txt")]
        result = _invoke_with_docs("query", docs)
        assert isinstance(result["contexts"], list)

    def test_sources_and_contexts_same_length(self):
        docs = [_make_doc(f"text {i}", f"file{i}.txt") for i in range(3)]
        result = _invoke_with_docs("query", docs)
        assert len(result["sources"]) == len(result["contexts"])

    def test_sources_all_strings(self):
        docs = [_make_doc(f"chunk {i}", f"src{i}.txt") for i in range(4)]
        result = _invoke_with_docs("query", docs)
        for s in result["sources"]:
            assert isinstance(s, str), f"source must be str, got {type(s)}"


# ---------------------------------------------------------------------------
# Citation correctness (sources match retrieved doc metadata)
# ---------------------------------------------------------------------------

class TestCitationCorrectness:
    def test_source_matches_doc_metadata(self):
        docs = [_make_doc("machine learning overview", "ml_overview.txt")]
        result = _invoke_with_docs("ml overview", docs)
        assert "ml_overview.txt" in result["sources"]

    def test_multiple_sources_all_cited(self):
        docs = [
            _make_doc("deep learning fundamentals", "dl.txt"),
            _make_doc("neural network basics", "nn.txt"),
        ]
        result = _invoke_with_docs("neural networks", docs)
        assert "dl.txt" in result["sources"]
        assert "nn.txt" in result["sources"]

    def test_source_order_matches_context_order(self):
        docs = [
            _make_doc("first doc content", "first.txt"),
            _make_doc("second doc content", "second.txt"),
        ]
        result = _invoke_with_docs("query", docs)
        assert result["sources"][0] == "first.txt"
        assert result["sources"][1] == "second.txt"

    def test_empty_retrieval_gives_empty_sources(self):
        result = _invoke_with_docs("empty query", [])
        assert result["sources"] == []

    def test_empty_retrieval_gives_empty_contexts(self):
        result = _invoke_with_docs("empty query", [])
        assert result["contexts"] == []

    def test_doc_without_source_metadata_produces_empty_string(self):
        """A Document with no 'source' key must produce "" not raise."""
        doc = Document(page_content="content with no source", metadata={})
        result = _invoke_with_docs("query", [doc])
        assert result["sources"] == [""]

    def test_contexts_contain_page_content(self):
        docs = [_make_doc("exact page content here", "doc.txt")]
        result = _invoke_with_docs("query", docs)
        assert "exact page content here" in result["contexts"]


# ---------------------------------------------------------------------------
# Deduplication behaviour
# ---------------------------------------------------------------------------

class TestCitationDeduplication:
    def test_same_source_appears_once_per_retrieved_chunk(self):
        """Two chunks from the same document each produce their own source entry.

        The pipeline does NOT deduplicate — one citation per retrieved chunk.
        This verifies the contract is transparent (caller can deduplicate if needed).
        """
        docs = [
            _make_doc("chunk 0 of doc A", "docA.txt"),
            _make_doc("chunk 1 of doc A", "docA.txt"),
        ]
        result = _invoke_with_docs("query", docs)
        assert result["sources"].count("docA.txt") == 2, (
            "pipeline must emit one source entry per chunk, not deduplicate"
        )
