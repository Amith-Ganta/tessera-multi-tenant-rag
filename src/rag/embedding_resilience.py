"""Embedding call wrapped in circuit breaker and bulkhead.

The bare embed_query() call in retriever_dense.py has no fault tolerance.
This module adds:
  - A dedicated circuit breaker (_embedding_breaker) so repeated embedding
    failures open the breaker and produce a fast EmbeddingUnavailable rather
    than piling up slow HTTP 500s.
  - The main_bulkhead so embedding concurrency counts against the shared pool.

Callers that receive EmbeddingUnavailable should fall back to sparse-only
retrieval where possible, or return HTTP 503 when no fallback exists.
"""

from __future__ import annotations

import logging

from src.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.resilience.bulkhead import main_bulkhead

_embedding_breaker = CircuitBreaker(name="embedding")
_log = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    """Raised when the embedding provider is unavailable and no fallback path exists."""


def embed_with_resilience(vectorstore, text: str) -> list[float]:
    """Embed text using the vectorstore's embedder, wrapped in circuit breaker and bulkhead.

    Raises EmbeddingUnavailable when the circuit is open.
    Any other provider exception is recorded by the breaker and re-raised so the
    caller can handle it (e.g. fall back to sparse retrieval).
    """
    try:
        with main_bulkhead:
            return _embedding_breaker.call(vectorstore.embeddings.embed_query, text)
    except CircuitOpenError as exc:
        _log.warning(
            "embedding circuit open; skipping dense retrieval",
            extra={"reason": str(exc)},
        )
        raise EmbeddingUnavailable("embedding provider unavailable") from exc
