"""Hybrid dense plus sparse retrieval with reciprocal rank fusion."""

from __future__ import annotations

from langchain_core.documents import Document

from .retriever_dense import retrieve_with_scores as retrieve_dense_scored
from .retriever_sparse import retrieve_sparse


def _dedupe_key(doc: Document) -> tuple[str, str]:
    source = str(doc.metadata.get("source", ""))
    return source, doc.page_content.strip()


def _rrf_rank(score: int, k_constant: int) -> float:
    return 1.0 / (k_constant + score)


def retrieve_hybrid(
    question: str,
    top_k: int,
    rrf_k: int = 60,
    return_scores: bool = False,
):
    """Fuse dense and sparse retrieval results with Reciprocal Rank Fusion.

    By default returns a list of Documents in fused (RRF) order, exactly as
    before. When return_scores=True, returns a list of (Document, score) tuples in
    the same fused order, where score is the DENSE cosine similarity for that
    document from the single first-pass dense retrieval (not the fused RRF score).
    Documents that appeared only in the sparse arm have no dense score and are
    reported with a dense similarity of 0.0.

    The dense arm runs once. Callers that need the top-1 similarity read it from
    the first returned tuple instead of issuing a second vector lookup.
    """

    dense_scored = retrieve_dense_scored(question, top_k=top_k)
    dense_docs = [doc for doc, _score in dense_scored]
    sparse_docs = retrieve_sparse(question, top_k=top_k)

    # Dense cosine similarity per document, keyed the same way we fuse. Used only
    # to attach the honest dense score when return_scores is requested; it never
    # affects fusion order.
    dense_scores: dict[tuple[str, str], float] = {}
    for doc, score in dense_scored:
        key = _dedupe_key(doc)
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = 0.0
        # Keep the best dense score if the same content appears more than once.
        if key not in dense_scores or value > dense_scores[key]:
            dense_scores[key] = value

    fused: dict[tuple[str, str], dict[str, object]] = {}

    for rank, doc in enumerate(dense_docs, start=1):
        key = _dedupe_key(doc)
        item = fused.setdefault(key, {"doc": doc, "score": 0.0})
        item["score"] = float(item["score"]) + _rrf_rank(rank, rrf_k)

    for rank, doc in enumerate(sparse_docs, start=1):
        key = _dedupe_key(doc)
        item = fused.setdefault(key, {"doc": doc, "score": 0.0})
        item["score"] = float(item["score"]) + _rrf_rank(rank, rrf_k)

    ranked = sorted(fused.values(), key=lambda item: float(item["score"]), reverse=True)
    top = ranked[:top_k]

    if not return_scores:
        return [item["doc"] for item in top]

    result: list[tuple[Document, float]] = []
    for item in top:
        doc = item["doc"]
        key = _dedupe_key(doc)
        result.append((doc, dense_scores.get(key, 0.0)))
    return result
