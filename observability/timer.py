"""Stage timing context manager and decorator.

``time_stage`` measures wall-clock time for a block with ``time.perf_counter``
and records the duration (in milliseconds) into the shared ``latency_store`` when
the block exits -- including when it exits by raising, so a stage that errors is
still counted rather than silently dropped.

Overhead budget (spec: < 5 ms per request added by instrumentation). By default
``time_stage`` does exactly two cheap things per stage: read the clock twice and
append to a bounded deque. That is sub-microsecond, so eight stages stay far
under the budget. Emitting child spans to LangSmith/Langfuse can touch a network
client, so it is gated behind ``TESSERA_STAGE_SPANS`` (off by default) and, even
when on, runs after the duration has been recorded and is fully exception-guarded
-- turning it on trades the budget for live tracing, a deliberate operator
choice rather than a default cost.
"""

from __future__ import annotations

import contextlib
import os
import time
from functools import wraps
from typing import Iterator, Optional

from .latency_store import latency_store, LatencyStore
from .stages import Stage, coerce_stage


def _spans_enabled() -> bool:
    return os.getenv("TESSERA_STAGE_SPANS", "").lower() in {"1", "true", "yes", "on"}


@contextlib.contextmanager
def time_stage(
    stage: "Stage | str",
    *,
    store: Optional[LatencyStore] = None,
    extra: Optional[dict] = None,
) -> Iterator[None]:
    """Time the enclosed block and record its duration against ``stage``.

    Args:
        stage: a ``Stage`` member or its string name.
        store: override the target store (tests inject a fresh one); defaults to
            the process-wide ``latency_store``.
        extra: optional metadata attached to the emitted span (never affects the
            recorded number).
    """
    stage = coerce_stage(stage)
    target = store or latency_store
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        # Record first -- this is the source of truth for percentiles and must
        # happen even if span emission below is slow or fails.
        target.record(stage, duration_ms)
        if _spans_enabled():
            try:
                from .tracing import emit_stage_spans

                emit_stage_spans(str(stage), duration_ms, extra)
            except Exception:
                pass


def stage_timer(stage: "Stage | str", *, store: Optional[LatencyStore] = None):
    """Decorator form: time an entire function call as one stage.

    Usage::

        @stage_timer(Stage.RERANKING)
        def rerank(...):
            ...
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with time_stage(stage, store=store):
                return func(*args, **kwargs)

        return wrapper

    return decorator
