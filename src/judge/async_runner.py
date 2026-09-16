"""Fire-and-forget async judge task.

submit_judge() wraps the synchronous evaluate_fn in asyncio.create_task so
that the /ask handler can return immediately with eval={"status": "pending"}.
The task writes its result (or error) into the process-wide judge_store and
updates the Langfuse span when observability is available.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Callable

from .judge_store import judge_store

logger = logging.getLogger(__name__)


async def _run_judge(
    trace_id: str,
    question: str,
    answer: str,
    contexts: list[str],
    evaluate_fn: Callable[..., dict],
) -> None:
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: evaluate_fn(question, answer, contexts),
        )
        result["status"] = "done"
        judge_store.set_result(trace_id, result)
        logger.info("async judge completed for trace_id=%s", trace_id)
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
) -> None:
    """Schedule a background judge task; returns immediately."""
    judge_store.set_pending(trace_id)
    asyncio.create_task(
        _run_judge(trace_id, question, answer, contexts, evaluate_fn),
        name=f"judge-{trace_id}",
    )
