"""Stand-alone judge worker process (Phase 5, Pattern 4: Message Queue).

Drains the Redis judge queue with a bounded pool of concurrent workers.
Each worker BRPOP-s a job, calls evaluate_fn, and writes the result back to
Redis.  The number of concurrent workers is capped at JUDGE_WORKER_CONCURRENCY
(default 3) so the OpenAI/DeepEval provider is not hammered.

Usage:
    uv run python judge_worker.py

Graceful shutdown: Ctrl-C waits for all in-flight jobs to finish before exit.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("judge_worker")


async def _process_job(job: dict, queue, evaluate_fn) -> None:
    trace_id = job.get("trace_id", "unknown")
    question = job.get("question", "")
    answer = job.get("answer", "")
    contexts = job.get("contexts", [])
    t0 = time.perf_counter()
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: evaluate_fn(question, answer, contexts),
        )
        result["status"] = "done"
        duration_ms = (time.perf_counter() - t0) * 1000
        queue.set_result(trace_id, result)
        logger.info("job done  trace=%s duration_ms=%.0f", trace_id, duration_ms)
    except Exception as exc:
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.error("job error trace=%s duration_ms=%.0f error=%s", trace_id, duration_ms, exc)
        queue.set_result(trace_id, {"status": "error", "reason": str(exc)})


async def _worker_loop(worker_id: int, queue, evaluate_fn, stop_event: asyncio.Event) -> None:
    logger.info("worker %d started", worker_id)
    while not stop_event.is_set():
        job = await asyncio.get_event_loop().run_in_executor(None, lambda: queue.blocking_pop(timeout=2))
        if job is None:
            continue
        await _process_job(job, queue, evaluate_fn)
    logger.info("worker %d stopped", worker_id)


async def main() -> None:
    from src.judge.redis_queue import judge_queue
    from src.rag.live_eval import evaluate_answer
    from src.rag.config import JUDGE_WORKER_CONCURRENCY

    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("shutdown signal received — draining in-flight jobs…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows does not support add_signal_handler for SIGTERM
            signal.signal(sig, _handle_signal)

    logger.info("judge worker starting — concurrency=%d", JUDGE_WORKER_CONCURRENCY)
    workers = [
        _worker_loop(i, judge_queue, evaluate_answer, stop_event)
        for i in range(JUDGE_WORKER_CONCURRENCY)
    ]
    await asyncio.gather(*workers)
    logger.info("judge worker exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
