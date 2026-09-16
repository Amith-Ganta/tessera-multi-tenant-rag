"""Fire-and-forget async judge task.

submit_judge() wraps the synchronous evaluate_fn in asyncio.create_task so
that the /ask handler can return immediately with eval={"status": "pending"}.
The task writes its result (or error) into the process-wide judge_store.

Phase 4b: when cache_key and cache_payload are supplied, the task also writes
the answer into semantic_cache AFTER the judge completes successfully (status=done).
A failed or errored judge never writes to cache.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, Callable

from .judge_store import judge_store

logger = logging.getLogger(__name__)


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
        logger.info("async judge completed for trace_id=%s", trace_id)

        # Write to semantic cache only after a successful (done) judge pass.
        if cache_key and cache_payload is not None:
            try:
                from src.rag.config import CACHE_ENABLED
                if CACHE_ENABLED:
                    from src.cache.semantic_cache import semantic_cache
                    semantic_cache.set(cache_key, cache_payload)
                    logger.debug("cache set for key=%s after async judge", cache_key[:16])
            except Exception:
                pass  # cache write failure is non-fatal
    except Exception:
        error_detail = traceback.format_exc()
        logger.error("async judge failed for trace_id=%s:\n%s", trace_id, error_detail)
        judge_store.set_result(trace_id, {"status": "error", "reason": error_detail})


def submit_judge(
    trace_id: str,
    question: str,
    answer: str,
    contexts: list[str],
    evaluate_fn: Callable[..., dict],
    cache_key: str | None = None,
    cache_payload: dict[str, Any] | None = None,
) -> None:
    """Schedule a background judge task; returns immediately.

    cache_key / cache_payload: when supplied, the semantic cache is populated
    after a successful judge run (Phase 4b async-mode cache write).
    """
    judge_store.set_pending(trace_id)
    asyncio.create_task(
        _run_judge(trace_id, question, answer, contexts, evaluate_fn, cache_key, cache_payload),
        name=f"judge-{trace_id}",
    )
