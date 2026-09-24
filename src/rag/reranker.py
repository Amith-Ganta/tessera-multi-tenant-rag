"""Cross-encoder reranking for retrieval candidates."""

from __future__ import annotations

from functools import lru_cache

from langchain_core.documents import Document


DEFAULT_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@lru_cache(maxsize=1)
def _get_model(model_name: str = DEFAULT_CROSS_ENCODER_MODEL):
    from sentence_transformers import CrossEncoder  # lazy: triggers torch, very slow import
    return CrossEncoder(model_name)


def rerank(question: str, docs: list[Document], top_k: int, model_name: str = DEFAULT_CROSS_ENCODER_MODEL) -> list[Document]:
    """Reorder documents by cross-encoder relevance and truncate to top_k."""

    if not docs:
        return []
    model = _get_model(model_name)
    pairs = [(question, doc.page_content) for doc in docs]
    scores = model.predict(pairs)
    ranked = sorted(zip(docs, scores), key=lambda item: float(item[1]), reverse=True)
    return [doc for doc, _ in ranked[:top_k]]
