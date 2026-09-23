"""RAG quality observability signals (Phase B4).

Provides record_rag_signals() — a single call sites use after every /ask
request to emit structured, queryable quality data into the analytics log.

Signals captured:
  retrieval  — context_count, dense_scores (min/max/mean), reranker_scores
  eval       — pass_rate (fraction of numeric metrics that passed), per-metric
               pass/fail/skip/error summary, any metric that is below threshold
  guard      — retries, final_outcome (passed | refined | insufficient_context)
  cost       — estimated_usd (from AskResponse if available)

All fields are optional: callers pass only what they have.  Missing fields are
omitted from the log record rather than filled with None, keeping the JSONL
rows compact.  All computation is pure Python — no I/O, no network calls — so
the function never raises in production.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _safe_stats(scores: list[float]) -> dict[str, float] | None:
    """Return min/max/mean for a non-empty list of floats, or None."""
    if not scores:
        return None
    n = len(scores)
    return {
        "min": round(min(scores), 4),
        "max": round(max(scores), 4),
        "mean": round(sum(scores) / n, 4),
        "count": n,
    }


def _eval_summary(metrics: dict[str, dict]) -> dict[str, Any]:
    """Derive pass_rate and a compact per-metric status map from the eval dict."""
    if not metrics:
        return {"pass_rate": None, "metrics": {}}

    numeric_pass = 0
    numeric_total = 0
    metric_status: dict[str, str] = {}

    for name, data in metrics.items():
        if not isinstance(data, dict):
            metric_status[name] = "invalid"
            continue
        if "error" in data:
            metric_status[name] = "error"
        elif data.get("status") == "skipped":
            metric_status[name] = "skipped"
        elif "score" in data:
            numeric_total += 1
            if data.get("passed", False):
                numeric_pass += 1
                metric_status[name] = "pass"
            else:
                metric_status[name] = "fail"
        else:
            metric_status[name] = "unknown"

    pass_rate = round(numeric_pass / numeric_total, 4) if numeric_total > 0 else None
    return {"pass_rate": pass_rate, "metrics": metric_status}


def record_rag_signals(
    *,
    tenant: str,
    strategy: str,
    latency_ms: float | None = None,
    context_count: int | None = None,
    dense_scores: list[float] | None = None,
    reranker_scores: list[float] | None = None,
    eval_metrics: dict[str, dict] | None = None,
    guard_retries: int | None = None,
    guard_outcome: str | None = None,
    estimated_cost_usd: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble and emit one RAG quality signal record to the analytics log.

    Returns the assembled record (useful for tests; the side-effect is the log
    write).  Never raises — exceptions are swallowed and logged at WARNING level.
    """
    try:
        record: dict[str, Any] = {
            "type": "rag_quality",
            "ts": time.time(),
            "tenant": tenant,
            "strategy": strategy,
        }

        if latency_ms is not None:
            record["latency_ms"] = round(latency_ms, 1)

        retrieval: dict[str, Any] = {}
        if context_count is not None:
            retrieval["context_count"] = context_count
        if dense_scores is not None:
            stats = _safe_stats([s for s in dense_scores if isinstance(s, (int, float))])
            if stats:
                retrieval["dense_scores"] = stats
        if reranker_scores is not None:
            stats = _safe_stats([s for s in reranker_scores if isinstance(s, (int, float))])
            if stats:
                retrieval["reranker_scores"] = stats
        if retrieval:
            record["retrieval"] = retrieval

        if eval_metrics is not None:
            record["eval"] = _eval_summary(eval_metrics)

        guard: dict[str, Any] = {}
        if guard_retries is not None:
            guard["retries"] = guard_retries
        if guard_outcome is not None:
            guard["outcome"] = guard_outcome
        if guard:
            record["guard"] = guard

        if estimated_cost_usd is not None:
            record["estimated_cost_usd"] = round(estimated_cost_usd, 6)

        if extra:
            record["extra"] = extra

        # Emit to the analytics JSONL log; import here to avoid circular imports.
        from src.rag.analytics import log_analytics
        log_analytics(record)

        return record

    except Exception as exc:
        logger.warning("record_rag_signals failed: %s", exc)
        return {}
