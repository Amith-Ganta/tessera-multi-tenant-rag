"""Fire-and-forget async judge task (Phase 4a / Phase 5).

Phase 4a: submit_judge() uses asyncio.create_task (in-process, unbounded).
Phase 5:  when JUDGE_QUEUE_ENABLED=True, submit_judge() publishes to a Redis
          queue instead.  A separate judge_worker.py process drains the queue
          with a bounded pool (JUDGE_WORKER_CONCURRENCY=3), fixing D2=6.

The /ask handler always returns immediately regardless of mode.  Result storage:
- Queue mode  → Redis (judge_queue.set_result / judge_queue.get_result)
- In-proc mode → in-memory judge_store (unchanged from Phase 4a)

Sync mode (JUDGE_MODE=sync) is unaffected: it calls guarded_answer() directly
and never goes through submit_judge().
"""

from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from typing import Any, Callable

from .judge_store import judge_store

logger = logging.getLogger(__name__)

_QUALITY_SIGNAL_METRICS = frozenset({"faithfulness", "answer_relevancy", "correctness"})


def _emit_judge_quality_signal(trace_id: str, result: dict) -> None:
    """Write a quality signal to analytics when any key metric fails its threshold.

    Called after every successful judge run so operators can detect quality
    drift without polling judge_store.  Never raises — analytics failure must
    not affect the caller.
    """
    try:
        metrics: dict = result.get("metrics", {})
        if not metrics:
            return
        failed = {
            name: {
                "score": m.get("score"),
                "threshold": m.get("threshold"),
                "reason": m.get("reason"),
            }
            for name, m in metrics.items()
            if name in _QUALITY_SIGNAL_METRICS
            and isinstance(m, dict)
            and m.get("passed") is False
        }
        if not failed:
            return
        from src.rag.analytics import log_analytics
        import datetime
        log_analytics({
            "event": "judge_quality_fail",
            "trace_id": trace_id,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "failed_metrics": failed,
        })
    except Exception:
        pass

# Tracks jobs published to the queue in the current process lifetime.
# Replaces the Phase 4a in-process active-task counter.
_published_judges = 0
_published_lock = threading.Lock()


def get_published_judges() -> int:
    return _published_judges


# ── in-process fallback (Phase 4a, used when queue is disabled) ──────────────

async def _run_judge(
    trace_id: str,
    question: str,
    answer: str,
    contexts: list[str],
    evaluate_fn: Callable[..., dict],
    cache_key: str | None,
    cache_payload: dict[str, Any] | None,
) -> None:
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: evaluate_fn(question, answer, contexts),
        )
        result["status"] = "done"
        judge_store.set_result(trace_id, result)
        logger.info("in-proc judge done trace_id=%s", trace_id)

        # Feed judge scores back into analytics so quality drift is observable.
        _emit_judge_quality_signal(trace_id, result)

        if cache_key and cache_payload is not None:
            try:
                from src.rag.config import CACHE_ENABLED
                if CACHE_ENABLED:
                    from src.cache.semantic_cache import semantic_cache
                    _tenant = cache_payload.get("_tenant", "")
                    if _tenant:
                        semantic_cache.set_tagged(cache_key, cache_payload, _tenant)
                    else:
                        semantic_cache.set(cache_key, cache_payload)
            except Exception:
                pass
    except Exception:
        error_detail = traceback.format_exc()
        logger.error("in-proc judge failed trace_id=%s:\n%s", trace_id, error_detail)
        judge_store.set_result(trace_id, {"status": "error", "reason": error_detail})


# ── public entry point ────────────────────────────────────────────────────────

def submit_judge(
    trace_id: str,
    question: str,
    answer: str,
    contexts: list[str],
    evaluate_fn: Callable[..., dict],
    cache_key: str | None = None,
    cache_payload: dict[str, Any] | None = None,
) -> dict:
    """Publish a judge job (Redis queue) or schedule in-process (fallback).

    Returns a status dict so callers can surface the actual eval state:
      {"status": "pending", "trace_id": <id>}   — job successfully queued or scheduled
      {"status": "unavailable", "reason": "queue_unavailable"}  — Redis publish failed

    Returns immediately in both modes so /ask latency is unaffected.
    cache_key / cache_payload are carried in the queue payload; the worker
    writes to semantic_cache after a successful judge run.
    """
    from src.rag.config import JUDGE_QUEUE_ENABLED

    global _published_judges

    # Always mark pending in the in-process store so GET /eval/{trace_id} can
    # return 202 while the worker processes the job.
    judge_store.set_pending(trace_id)

    if JUDGE_QUEUE_ENABLED:
        from .redis_queue import judge_queue
        job_payload = {
            "question": question,
            "answer": answer,
            "contexts": contexts,
            "cache_key": cache_key,
            "cache_payload": cache_payload,
        }
        published = judge_queue.publish(trace_id, job_payload)
        if published:
            with _published_lock:
                _published_judges += 1
            logger.debug("judge job queued trace_id=%s", trace_id)
            return {"status": "pending", "trace_id": trace_id}
        else:
            logger.warning(
                "judge queue publish failed trace_id=%s",
                trace_id,
                extra={"trace_id": trace_id},
            )
            return {"status": "unavailable", "reason": "queue_unavailable"}
    else:
        asyncio.create_task(
            _run_judge(trace_id, question, answer, contexts, evaluate_fn, cache_key, cache_payload),
            name=f"judge-{trace_id}",
        )
        return {"status": "pending", "trace_id": trace_id}
