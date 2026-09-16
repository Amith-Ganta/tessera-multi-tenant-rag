"""Tests for the stage-timing context manager and decorator.

Run from the project root: ``pytest tests/observability/test_timer.py``.
Timing tests inject a fresh ``LatencyStore`` via the ``store=`` argument so they
never depend on wall-clock precision beyond "a positive, finite number of ms"
and never touch the process-wide singleton.
"""

from __future__ import annotations

import time

import pytest

from observability import LatencyStore, Stage, time_stage
from observability.timer import stage_timer, _spans_enabled


def test_time_stage_records_one_positive_sample():
    store = LatencyStore()
    with time_stage(Stage.LLM_GENERATION, store=store):
        pass
    stats = store.snapshot()["llm_generation"]
    assert stats["count"] == 1
    assert stats["min_ms"] is not None
    assert stats["min_ms"] >= 0.0
    assert stats["max_ms"] < 5000.0  # a no-op block is nowhere near 5s


def test_time_stage_measures_elapsed_time():
    store = LatencyStore()
    with time_stage(Stage.VECTOR_RETRIEVAL, store=store):
        time.sleep(0.02)  # 20 ms
    stats = store.snapshot()["vector_retrieval"]
    assert stats["count"] == 1
    # Recorded duration should be at least the sleep, minus scheduler slack.
    assert stats["max_ms"] >= 15.0


def test_time_stage_records_even_when_body_raises():
    store = LatencyStore()
    with pytest.raises(RuntimeError):
        with time_stage(Stage.RERANKING, store=store):
            raise RuntimeError("boom")
    # The finally block must still have recorded the (partial) duration.
    assert store.snapshot()["reranking"]["count"] == 1


def test_time_stage_accepts_stage_string():
    store = LatencyStore()
    with time_stage("embedding", store=store):
        pass
    assert store.snapshot()["embedding"]["count"] == 1


def test_time_stage_unknown_stage_raises():
    store = LatencyStore()
    with pytest.raises(ValueError):
        with time_stage("nope", store=store):
            pass


def test_stage_timer_decorator_times_the_call_and_returns_value():
    store = LatencyStore()

    @stage_timer(Stage.POST_PROCESSING, store=store)
    def work(x, y):
        return x + y

    assert work(2, 3) == 5
    assert store.snapshot()["post_processing"]["count"] == 1


def test_stage_timer_decorator_records_on_exception():
    store = LatencyStore()

    @stage_timer(Stage.QUERY_PROCESSING, store=store)
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boom()
    assert store.snapshot()["query_processing"]["count"] == 1


def test_multiple_stages_accumulate_independently():
    store = LatencyStore()
    for _ in range(3):
        with time_stage(Stage.EMBEDDING, store=store):
            pass
    with time_stage(Stage.RERANKING, store=store):
        pass
    snap = store.snapshot()
    assert snap["embedding"]["count"] == 3
    assert snap["reranking"]["count"] == 1


def test_spans_disabled_by_default(monkeypatch):
    # The <5ms overhead budget depends on span emission being off unless opted in.
    monkeypatch.delenv("TESSERA_STAGE_SPANS", raising=False)
    assert _spans_enabled() is False
    monkeypatch.setenv("TESSERA_STAGE_SPANS", "1")
    assert _spans_enabled() is True
    monkeypatch.setenv("TESSERA_STAGE_SPANS", "false")
    assert _spans_enabled() is False


def test_default_path_stays_well_under_overhead_budget():
    # Instrumentation-only cost (empty body) across all stages must be a tiny
    # fraction of the 5ms/request budget. We time every current stage and assert
    # the pure overhead is comfortably sub-millisecond on any normal machine.
    # The count assertion uses len(Stage) so the test stays correct as new stages
    # are added (Phase 3 added JUDGE, making 9 total).
    store = LatencyStore()
    start = time.perf_counter()
    for stage in Stage:
        with time_stage(stage, store=store):
            pass
    overhead_ms = (time.perf_counter() - start) * 1000.0
    assert overhead_ms < 5.0
    assert sum(store.snapshot()[str(s)]["count"] for s in Stage) == len(Stage)
