"""Per-stage latency observability for the Tessera RAG pipeline (Phase 1).

Public surface:

    from observability import Stage, time_stage, latency_store

`Stage` enumerates the eight RAG stages. `time_stage` is a context manager that
records how long a stage took into the in-memory `latency_store` (a bounded ring
buffer) and, when configured, emits a child span to LangSmith and/or Langfuse.
`latency_store.snapshot()` returns per-stage P50/P95/P99 for the /metrics/latency
endpoint. Every tracing side effect is optional and no-op safe: with no keys set
and no tracing packages installed, only the local ring buffer is touched, so the
request path never depends on a network call.
"""

from __future__ import annotations

from .stages import Stage, ALL_STAGES
from .latency_store import latency_store, LatencyStore
from .timer import time_stage, stage_timer

__all__ = [
    "Stage",
    "ALL_STAGES",
    "latency_store",
    "LatencyStore",
    "time_stage",
    "stage_timer",
]
