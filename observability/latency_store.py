"""In-memory ring buffer of recent per-stage latencies, with percentile readout.

Design constraints (Phase 1 spec):

* Keep only the last N samples per stage (default 1000): a bounded
  ``collections.deque(maxlen=N)`` per stage drops the oldest sample on overflow
  in O(1), so memory is fixed regardless of uptime.
* No new dependencies: percentiles come from the stdlib ``statistics.quantiles``.
* Thread safe: FastAPI/uvicorn serves requests from a worker thread pool, so
  record() and snapshot() take a lock. The critical section is a deque append or
  a bounded copy, so contention is negligible.

This is a per-process store. It is intentionally not shared across workers or
pods; aggregating across replicas is a later-phase concern (ship metrics to a
real backend). For a single-process app it gives exact recent percentiles for
free.
"""

from __future__ import annotations

import statistics
import threading
from collections import deque
from typing import Deque, Dict

from .stages import Stage, ALL_STAGES, coerce_stage

DEFAULT_MAX_SAMPLES = 1000


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Percentile of an already-sorted, non-empty list, via stdlib quantiles.

    ``statistics.quantiles`` needs at least two data points and returns the n-1
    interior cut points (it never returns the min or the max), so we handle the
    small-sample and boundary cases explicitly:

    * one sample  -> that sample is every percentile
    * pct <= 0    -> the minimum;  pct >= 100 -> the maximum
    * otherwise   -> the appropriate cut from ``quantiles(n=100, inclusive)``,
      which places cut point i at the i-th hundredth of the range and matches
      the common "nearest-rank on a continuous distribution" reading closely.
    """
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    if pct <= 0:
        return sorted_values[0]
    if pct >= 100:
        return sorted_values[-1]
    # cuts[i] is the (i+1)-th percentile boundary for i in 0..98.
    cuts = statistics.quantiles(sorted_values, n=100, method="inclusive")
    idx = int(round(pct)) - 1
    idx = max(0, min(idx, len(cuts) - 1))
    return cuts[idx]


class LatencyStore:
    """Bounded per-stage sample buffers plus percentile snapshots."""

    def __init__(self, max_samples: int = DEFAULT_MAX_SAMPLES):
        self._max_samples = int(max_samples)
        self._lock = threading.Lock()
        # One deque per stage, pre-created so every stage always appears in a
        # snapshot (with count 0) even before it has recorded a sample.
        self._buffers: Dict[Stage, Deque[float]] = {
            stage: deque(maxlen=self._max_samples) for stage in ALL_STAGES
        }

    @property
    def max_samples(self) -> int:
        return self._max_samples

    def record(self, stage: "Stage | str", duration_ms: float) -> None:
        """Append one stage duration (milliseconds). Ignores negatives/NaN."""
        stage = coerce_stage(stage)
        value = float(duration_ms)
        if value < 0 or value != value:  # drop negatives and NaN
            return
        with self._lock:
            self._buffers[stage].append(value)

    def _stats_for(self, values: list[float]) -> dict:
        if not values:
            return {
                "count": 0,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "min_ms": None,
                "max_ms": None,
                "mean_ms": None,
            }
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "p50_ms": round(_percentile(ordered, 50), 3),
            "p95_ms": round(_percentile(ordered, 95), 3),
            "p99_ms": round(_percentile(ordered, 99), 3),
            "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
            "mean_ms": round(statistics.fmean(ordered), 3),
        }

    def snapshot(self) -> dict:
        """Per-stage {count, p50/p95/p99/min/max/mean} for every stage.

        Takes a shallow copy of each buffer under the lock, then computes
        percentiles outside the lock so a large buffer does not hold the lock
        during the sort.
        """
        with self._lock:
            copied = {stage: list(buf) for stage, buf in self._buffers.items()}
        return {str(stage): self._stats_for(values) for stage, values in copied.items()}

    def reset(self) -> None:
        """Clear every buffer. Used by tests and by an explicit admin reset."""
        with self._lock:
            for buf in self._buffers.values():
                buf.clear()


# Process-wide singleton the pipeline and the /metrics/latency endpoint share.
latency_store = LatencyStore()
