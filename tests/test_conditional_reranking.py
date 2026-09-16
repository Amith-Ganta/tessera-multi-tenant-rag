"""Tests for Phase 2 Part A -- conditional cross-encoder re-ranking.

Run from the project root: ``pytest tests/test_conditional_reranking.py``.

These tests exercise the decision layer ``strategies._maybe_rerank`` in isolation.
The real cross-encoder is never loaded: ``strategies.rerank`` is monkeypatched with
a spy so each test asserts *whether* the re-ranker was called (the only thing Part A
changes) and, where relevant, that the re-ranker's re-ordering is preserved when it
does run. The gate reads ``RERANK_SKIP_THRESHOLD`` from config, never a literal.
"""

from __future__ import annotations

from langchain_core.documents import Document

from src.rag import strategies
from src.rag.config import RERANK_SKIP_THRESHOLD


def _docs(*texts: str) -> list[Document]:
    return [Document(page_content=t, metadata={"source": f"{t}.md"}) for t in texts]


class _RerankSpy:
    """Stand-in for the cross-encoder: records calls and reverses the order.

    Reversing is an observable, deterministic re-ordering, so a test can prove the
    re-ranker's output actually flowed through (not just that it was invoked).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, question: str, docs: list[Document], top_k: int) -> list[Document]:
        self.calls.append((question, top_k))
        return list(reversed(docs))[:top_k]


def test_high_confidence_skips_reranker(monkeypatch):
    """top1 = 0.95 (> 0.90) -> re-ranker is skipped, retrieval order passes through."""
    spy = _RerankSpy()
    monkeypatch.setattr(strategies, "rerank", spy)

    docs = _docs("a", "b", "c")
    trace: list[str] = []
    out = strategies._maybe_rerank("q", docs, top_k=3, top1=0.95, trace=trace)

    assert spy.calls == []  # never called
    assert [d.page_content for d in out] == ["a", "b", "c"]  # order unchanged
    assert any("action=skipped" in line and "reason=high_confidence" in line for line in trace)


def test_low_confidence_runs_reranker(monkeypatch):
    """top1 = 0.72 (< 0.90) -> re-ranker runs exactly as before."""
    spy = _RerankSpy()
    monkeypatch.setattr(strategies, "rerank", spy)

    docs = _docs("a", "b", "c")
    trace: list[str] = []
    out = strategies._maybe_rerank("q", docs, top_k=3, top1=0.72, trace=trace)

    assert spy.calls == [("q", 3)]  # called once
    assert [d.page_content for d in out] == ["c", "b", "a"]  # spy reversed order
    assert any("action=ran" in line for line in trace)


def test_exactly_at_threshold_runs_reranker(monkeypatch):
    """top1 == threshold -> RUNS. Skip only when strictly above the threshold."""
    spy = _RerankSpy()
    monkeypatch.setattr(strategies, "rerank", spy)

    docs = _docs("a", "b")
    trace: list[str] = []
    strategies._maybe_rerank("q", docs, top_k=2, top1=RERANK_SKIP_THRESHOLD, trace=trace)

    assert spy.calls == [("q", 2)]  # 0.90 exactly still re-ranks
    assert any("action=ran" in line for line in trace)


def test_empty_retrieval_skips_safely(monkeypatch):
    """Zero retrieved docs -> skip safely, no re-ranker call, no exception."""
    spy = _RerankSpy()
    monkeypatch.setattr(strategies, "rerank", spy)

    trace: list[str] = []
    out = strategies._maybe_rerank("q", [], top_k=5, top1=0.10, trace=trace)

    assert out == []
    assert spy.calls == []  # empty short-circuits before any model work
    assert any("action=skipped" in line and "reason=no_results" in line for line in trace)


def test_reranker_boosts_recall_on_ambiguous_queries(monkeypatch):
    """On an ambiguous (low-confidence) query set the re-ranker is invoked and its
    re-ordering wins.

    Fixture: the relevant chunk arrives LAST from fused retrieval; a re-ranker that
    surfaces it must be allowed to run, otherwise the good chunk stays buried. This
    is the recall-boost property Part A must not sacrifice on low-confidence queries.
    """
    def relevance_first(question: str, docs: list[Document], top_k: int) -> list[Document]:
        # A faithful cross-encoder promotes the chunk that mentions the query term.
        ranked = sorted(docs, key=lambda d: question not in d.page_content)
        return ranked[:top_k]

    monkeypatch.setattr(strategies, "rerank", relevance_first)

    ambiguous_queries = ["alpha", "beta", "gamma"]
    for term in ambiguous_queries:
        # Retrieval order buries the relevant chunk at the end.
        retrieved = _docs("noise one", "noise two", f"the {term} answer")
        out = strategies._maybe_rerank(term, retrieved, top_k=3, top1=0.40)
        # Low confidence -> re-ranker ran -> relevant chunk is now rank 1.
        assert term in out[0].page_content


def test_skip_decision_is_logged_with_reason(monkeypatch):
    """The skip decision emits the exact structured reason string the task requires."""
    monkeypatch.setattr(strategies, "rerank", _RerankSpy())

    trace: list[str] = []
    strategies._maybe_rerank("q", _docs("a"), top_k=1, top1=0.99, trace=trace)

    line = next(l for l in trace if "stage=reranking" in l)
    assert "action=skipped" in line
    assert "reason=high_confidence" in line
    assert "top1=0.99" in line


# ---------------------------------------------------------------------------
# Phase 3 Part 2 -- single retrieval pass; retrieve_hybrid can return scores,
# and the adaptive path no longer issues a second vector-store lookup.
# ---------------------------------------------------------------------------

from src.rag import retriever_dense, retriever_hybrid  # noqa: E402


class _FakeEmbeddings:
    """Minimal embeddings stub — returns a fixed-length zero vector.

    retriever_dense._embed_query calls vectorstore.embeddings.embed_query(question).
    The vector value does not matter for counting purposes; we just need a list so
    similarity_search_by_vector_with_relevance_scores gets a valid argument.
    """

    def embed_query(self, text: str) -> list[float]:
        return [0.0] * 4


class _FakeVectorStore:
    """Counts every scored similarity query so a test can prove exactly one happened.

    retriever_dense now calls:
      - vectorstore.embeddings.embed_query(question)  [for the embedding]
      - vectorstore.similarity_search_by_vector_with_relevance_scores(embedding, k)
      - vectorstore.similarity_search_by_vector(embedding, k)  [non-scored path]

    We match those exact method names and signatures so the fake is actually called.
    """

    def __init__(self, docs_with_scores: list[tuple[Document, float]]) -> None:
        self._scored = docs_with_scores
        self.embeddings = _FakeEmbeddings()
        self.query_count = 0

    def similarity_search_by_vector_with_relevance_scores(
        self, embedding: list[float], k: int
    ):
        self.query_count += 1
        return list(self._scored)[:k]

    def similarity_search_by_vector(self, embedding: list[float], k: int):
        self.query_count += 1
        return [doc for doc, _ in self._scored][:k]


def _install_fake_store(monkeypatch, scored):
    store = _FakeVectorStore(scored)
    monkeypatch.setattr(retriever_dense, "get_vectorstore", lambda: store)
    # Isolate the dense arm: no BM25/corpus access during these tests.
    monkeypatch.setattr(retriever_hybrid, "retrieve_sparse", lambda q, top_k: [])
    return store


def test_return_scores_true_yields_document_score_tuples(monkeypatch):
    """return_scores=True -> list of (Document, float) with the dense scores."""
    scored = [(Document(page_content="a", metadata={"source": "a.md"}), 0.83),
              (Document(page_content="b", metadata={"source": "b.md"}), 0.41)]
    _install_fake_store(monkeypatch, scored)

    out = retriever_hybrid.retrieve_hybrid("q", top_k=2, return_scores=True)

    assert isinstance(out, list) and out
    for item in out:
        assert isinstance(item, tuple) and len(item) == 2
        doc, score = item
        assert isinstance(doc, Document)
        assert isinstance(score, float)
    # The top-ranked tuple carries the top-1 dense similarity from the first pass.
    assert out[0][1] == 0.83


def test_return_scores_false_yields_bare_documents(monkeypatch):
    """Default (return_scores=False) is unchanged: a list of Documents."""
    scored = [(Document(page_content="a", metadata={"source": "a.md"}), 0.83),
              (Document(page_content="b", metadata={"source": "b.md"}), 0.41)]
    _install_fake_store(monkeypatch, scored)

    out = retriever_hybrid.retrieve_hybrid("q", top_k=2)

    assert out and all(isinstance(d, Document) for d in out)
    assert not any(isinstance(d, tuple) for d in out)


def test_adaptive_path_queries_vectorstore_exactly_once(monkeypatch):
    """The adaptive path must retrieve once, not twice.

    Before Phase 3 the adaptive path hit the vector store twice per request (once
    for retrieval, once to recover the top-1 score). It now reuses the first-pass
    scores, so the store is queried exactly once.
    """
    scored = [(Document(page_content="a", metadata={"source": "a.md"}), 0.72),
              (Document(page_content="b", metadata={"source": "b.md"}), 0.30)]
    store = _install_fake_store(monkeypatch, scored)

    # Keep the rest of the pipeline off the network: skip the re-ranker and stub
    # the LLM so only retrieval is exercised.
    monkeypatch.setattr(strategies, "rerank", _RerankSpy())
    monkeypatch.setattr(strategies, "_llm", lambda model, messages, **kw: ("ans", strategies._zero_usage()))

    result = strategies._adaptive_impl("q", top_k=2, model="m", force_route="vector")

    assert store.query_count == 1
    # Sanity: the reused first-pass score reached the guard signal.
    assert result["top1_similarity"] == 0.72
