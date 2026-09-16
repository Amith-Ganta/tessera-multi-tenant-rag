"""Tests for the per-stage latency ring buffer and its percentile readout.

Run from the project root: ``pytest tests/observability/test_latency_store.py``.
Each test uses its own ``LatencyStore`` instance so the process-wide singleton is
never mutated and tests stay order-independent.
"""

from __future__ import annotations

import math

import pytest

from observability import LatencyStore
from observability.stages import Stage, ALL_STAGES
from observability.latency_store import _percentile


def test_empty_store_reports_every_stage_with_zero_count():
    store = LatencyStore()
    snap = store.snapshot()
    # Every canonical stage appears, even before any sample is recorded.
    assert set(snap.keys()) == {str(s) for s in ALL_STAGES}
    for stats in snap.values():
        assert stats["count"] == 0
        assert stats["p50_ms"] is None
        assert stats["p95_ms"] is None
        assert stats["p99_ms"] is None
        assert stats["min_ms"] is None
        assert stats["max_ms"] is None
        assert stats["mean_ms"] is None


def test_single_sample_is_every_percentile():
    store = LatencyStore()
    store.record(Stage.RERANKING, 12.5)
    stats = store.snapshot()["reranking"]
    assert stats["count"] == 1
    assert stats["p50_ms"] == 12.5
    assert stats["p95_ms"] == 12.5
    assert stats["p99_ms"] == 12.5
    assert stats["min_ms"] == 12.5
    assert stats["max_ms"] == 12.5
    assert stats["mean_ms"] == 12.5


def test_percentiles_on_uniform_1_to_100():
    # 1..100: p50 near 50, p95 near 95, p99 near 99, min=1, max=100.
    store = LatencyStore()
    for v in range(1, 101):
        store.record(Stage.LLM_GENERATION, float(v))
    stats = store.snapshot()["llm_generation"]
    assert stats["count"] == 100
    assert stats["min_ms"] == 1.0
    assert stats["max_ms"] == 100.0
    # Allow a small tolerance: the exact cut depends on the interpolation method,
    # but percentiles of 1..100 must land within a couple of units of the rank.
    assert abs(stats["p50_ms"] - 50) <= 2
    assert abs(stats["p95_ms"] - 95) <= 2
    assert abs(stats["p99_ms"] - 99) <= 2
    assert stats["p95_ms"] >= stats["p50_ms"]
    assert stats["p99_ms"] >= stats["p95_ms"]


def test_ring_buffer_evicts_oldest_beyond_max_samples():
    # A tiny window makes eviction observable: after writing 1..10 into a window
    # of 3, only the last three (8,9,10) survive.
    store = LatencyStore(max_samples=3)
    assert store.max_samples == 3
    for v in range(1, 11):
        store.record(Stage.EMBEDDING, float(v))
    stats = store.snapshot()["embedding"]
    assert stats["count"] == 3
    assert stats["min_ms"] == 8.0
    assert stats["max_ms"] == 10.0


def test_negative_and_nan_durations_are_dropped():
    store = LatencyStore()
    store.record(Stage.QUERY_PROCESSING, -1.0)
    store.record(Stage.QUERY_PROCESSING, float("nan"))
    store.record(Stage.QUERY_PROCESSING, 5.0)
    stats = store.snapshot()["query_processing"]
    assert stats["count"] == 1
    assert stats["min_ms"] == 5.0


def test_record_accepts_stage_string_alias():
    store = LatencyStore()
    store.record("vector_retrieval", 3.0)  # string, not the Stage member
    assert store.snapshot()["vector_retrieval"]["count"] == 1


def test_unknown_stage_string_raises():
    store = LatencyStore()
    with pytest.raises(ValueError):
        store.record("not_a_real_stage", 1.0)


def test_reset_clears_all_buffers():
    store = LatencyStore()
    for s in ALL_STAGES:
        store.record(s, 1.0)
    store.reset()
    snap = store.snapshot()
    assert all(stats["count"] == 0 for stats in snap.values())


def test_stages_are_isolated_from_each_other():
    store = LatencyStore()
    store.record(Stage.EMBEDDING, 2.0)
    store.record(Stage.EMBEDDING, 4.0)
    store.record(Stage.RERANKING, 100.0)
    snap = store.snapshot()
    assert snap["embedding"]["count"] == 2
    assert snap["embedding"]["mean_ms"] == 3.0
    assert snap["reranking"]["count"] == 1
    assert snap["reranking"]["mean_ms"] == 100.0
    # Untouched stage still reports zero.
    assert snap["post_processing"]["count"] == 0


def test_percentile_helper_boundaries():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(values, 0) == 1.0      # <=0 -> minimum
    assert _percentile(values, 100) == 5.0    # >=100 -> maximum
    assert _percentile([7.0], 50) == 7.0      # single value -> itself
    mid = _percentile(values, 50)
    assert 1.0 <= mid <= 5.0


def test_snapshot_values_are_json_friendly():
    store = LatencyStore()
    store.record(Stage.PROMPT_STITCHING, 1.234567)
    stats = store.snapshot()["prompt_stitching"]
    # Rounded to 3 decimals and finite -- safe to serialize in the endpoint.
    assert stats["p50_ms"] == 1.235
    assert math.isfinite(stats["mean_ms"])
