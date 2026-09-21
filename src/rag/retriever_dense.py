"""Dense retrieval over the persisted Chroma index."""

from __future__ import annotations

import logging
import threading

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from .config import EMBEDDING_MODEL, RETRIEVER_TOP_K, get_openai_api_key
from .embedding_resilience import embed_with_resilience
from .tenant_context import active_index_dir

# Phase 3 latency instrumentation. Import is guarded the same way strategies.py
# guards it: if the top-level observability package is unavailable for any reason,
# time_stage degrades to a no-op context manager so retrieval behaves exactly as
# before. Instrumentation must never be able to break a request.
try:
    from observability import Stage, time_stage
except Exception:  # pragma: no cover - defensive import
    import contextlib as _contextlib

    class Stage:  # minimal shim; attribute access returns the stage name string
        EMBEDDING = "embedding"
        VECTOR_RETRIEVAL = "vector_retrieval"

    @_contextlib.contextmanager
    def time_stage(*_args, **_kwargs):
        yield

logger = logging.getLogger(__name__)

# Process-wide vectorstore cache. Building a Chroma client plus an OpenAIEmbeddings
# object on every call is expensive (a fresh embeddings handshake and a fresh
# on-disk index open per request), and the adaptive path used to do it twice per
# request. We construct each store once and reuse it. The cache is keyed by the
# resolved on-disk index directory so multi-tenant isolation is preserved: two
# calls in the same tenant context return the exact same object, while a different
# tenant's index gets its own store. Access is guarded by a lock so that under
# concurrency the store is still built exactly once per key.
_VECTORSTORE_CACHE: dict[str, Chroma] = {}
_VECTORSTORE_LOCK = threading.Lock()
_CONSTRUCTED_ONCE = False


def _build_vectorstore() -> Chroma:
    get_openai_api_key()
    embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL)
    return Chroma(persist_directory=str(active_index_dir()), embedding_function=embeddings)


def get_vectorstore() -> Chroma:
    """Return a cached Chroma store for the active tenant's index.

    Constructed once per process (per tenant index directory), not once per call.
    Callers use the returned object exactly as before; the return type is unchanged.
    """
    global _CONSTRUCTED_ONCE

    key = str(active_index_dir())

    cached = _VECTORSTORE_CACHE.get(key)
    if cached is not None:
        return cached

    with _VECTORSTORE_LOCK:
        # Re-check inside the lock: another thread may have built it while we waited.
        cached = _VECTORSTORE_CACHE.get(key)
        if cached is not None:
            return cached

        vectorstore = _build_vectorstore()
        _VECTORSTORE_CACHE[key] = vectorstore
        if not _CONSTRUCTED_ONCE:
            _CONSTRUCTED_ONCE = True
            logger.info("vectorstore constructed once")
            print("vectorstore constructed once")
        return vectorstore


def reset_vectorstore_cache() -> None:
    """Clear the cached vectorstore(s). Intended for tests.

    Forces the next get_vectorstore() call to construct a fresh store.
    """
    global _CONSTRUCTED_ONCE
    with _VECTORSTORE_LOCK:
        _VECTORSTORE_CACHE.clear()
        _CONSTRUCTED_ONCE = False


def _embed_query(vectorstore: Chroma, question: str):
    """Embed the query under the EMBEDDING stage timer.

    Phase 3 (3a): the query-embedding round-trip is the OpenAI network call that
    used to hide inside vector_retrieval. Timing it here, then querying Chroma by
    the resulting vector, gives embedding its own honest samples and drops that
    time out of vector_retrieval.
    """
    with time_stage(Stage.EMBEDDING):
        return embed_with_resilience(vectorstore, question)


def retrieve(question: str, top_k: int = RETRIEVER_TOP_K):
    vectorstore = get_vectorstore()
    embedding = _embed_query(vectorstore, question)
    with time_stage(Stage.VECTOR_RETRIEVAL):
        return vectorstore.similarity_search_by_vector(embedding, k=top_k)


def retrieve_with_scores(question: str, top_k: int = RETRIEVER_TOP_K):
    """Dense retrieval returning (Document, cosine_similarity) pairs.

    Uses the same cached store as retrieve(); the returned score is the dense
    relevance score in [0, 1]. Callers that need both documents and their dense
    similarity should use this so the pipeline never does a second vector lookup
    just to recover a score. The query embedding is timed under EMBEDDING and the
    vector-store lookup under VECTOR_RETRIEVAL.
    """
    vectorstore = get_vectorstore()
    embedding = _embed_query(vectorstore, question)
    with time_stage(Stage.VECTOR_RETRIEVAL):
        return vectorstore.similarity_search_by_vector_with_relevance_scores(
            embedding, k=top_k
        )
