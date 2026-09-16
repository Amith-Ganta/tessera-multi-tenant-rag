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
