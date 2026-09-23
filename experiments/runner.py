"""Retrieval experiment runner.

Runs a list of (query, expected_doc_ids) pairs through the retrieval config,
records per-query precision/recall, and returns aggregates.  When
``config.embedder_fn`` is set, that callable is used in place of real
embedding calls so experiments can be run offline without API access.

No real LLM or embedding API calls are made if ``embedder_fn`` is provided.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from experiments.config import ExperimentConfig


def _default_embedder(text: str) -> list[float]:
    raise RuntimeError(
        "No embedder_fn provided and no real embedder configured. "
        "Pass embedder_fn in ExperimentConfig or set up a real embedder."
    )


def _precision_at_k(retrieved: list[str], relevant: set[str]) -> float:
    if not retrieved:
        return 0.0
    hits = sum(1 for doc_id in retrieved if doc_id in relevant)
    return hits / len(retrieved)


def _recall_at_k(retrieved: list[str], relevant: set[str]) -> float:
    if not relevant:
        return 1.0
    hits = sum(1 for doc_id in retrieved if doc_id in relevant)
    return hits / len(relevant)


QueryCase = tuple[str, list[str]]  # (query_text, expected_doc_ids)


def run_experiment(
    config: ExperimentConfig,
    cases: Sequence[QueryCase],
    retrieve_fn: Callable[[list[float], int, str], list[str]],
) -> dict:
    """Run retrieval experiment.

    Parameters
    ----------
    config:
        ExperimentConfig instance; ``embedder_fn`` is used if set.
    cases:
        List of (query_text, expected_doc_ids) tuples.
    retrieve_fn:
        Callable(embedding_vector, top_k, similarity_metric) -> list[doc_id].
        In tests this is a mock; in production it calls the Chroma vectorstore.

    Returns
    -------
    dict with ``config``, ``per_query`` results, and ``aggregates``.
    """
    embedder = config.embedder_fn or _default_embedder

    per_query = []
    for query_text, expected_ids in cases:
        t0 = time.perf_counter()
        embedding = embedder(query_text)
        retrieved = retrieve_fn(embedding, config.top_k, config.similarity_metric)
        latency_ms = (time.perf_counter() - t0) * 1000

        relevant = set(expected_ids)
        p_at_k = _precision_at_k(retrieved, relevant)
        r_at_k = _recall_at_k(retrieved, relevant)

        per_query.append({
            "query": query_text,
            "expected": expected_ids,
            "retrieved": retrieved,
            "precision_at_k": p_at_k,
            "recall_at_k": r_at_k,
            "latency_ms": latency_ms,
        })

    count = len(per_query)
    if count == 0:
        aggregates = {
            "count": 0,
            "mean_precision_at_k": 0.0,
            "mean_recall_at_k": 0.0,
            "mean_latency_ms": 0.0,
        }
    else:
        aggregates = {
            "count": count,
            "mean_precision_at_k": sum(r["precision_at_k"] for r in per_query) / count,
            "mean_recall_at_k": sum(r["recall_at_k"] for r in per_query) / count,
            "mean_latency_ms": sum(r["latency_ms"] for r in per_query) / count,
        }

    return {
        "config": config.to_dict(),
        "per_query": per_query,
        "aggregates": aggregates,
    }
