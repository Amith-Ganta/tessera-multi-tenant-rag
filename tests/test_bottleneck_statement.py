"""Item 23: Bottleneck statement tests.

Covers:
- ALL_STAGES contains the expected pipeline stages (9 total)
- Stage enum values are plain strings (usable as dict keys / JSON)
- coerce_stage() accepts Stage enum and plain string
- coerce_stage() raises ValueError on unknown stage
- LatencyStore.record() ignores negatives and NaN
- LatencyStore.snapshot() returns all stages even with zero samples
- Zero-sample stats: all percentile fields are None, count is 0
- Single-sample stats: all percentiles equal that sample
- Percentile ordering: p99 >= p95 >= p50
- Stage with highest p95 is the identified bottleneck
- reset() clears all buffers
- Thread-safe concurrent record() calls

No real LLM or network calls are made.
"""

from __future__ import annotations

import math
import threading


class TestStageDefinitions:
    """ALL_STAGES and Stage enum contract."""

    def test_all_stages_count_is_nine(self):
        from observability.stages import ALL_STAGES
        assert len(ALL_STAGES) == 9

    def test_expected_stages_present(self):
        from observability.stages import Stage
        expected = {
            "query_processing", "embedding", "vector_retrieval",
            "metadata_filtering", "reranking", "prompt_stitching",
            "llm_generation", "post_processing", "judge",
        }
        actual = {s.value for s in Stage}
        assert actual == expected

    def test_stage_str_is_value_not_enum_name(self):
        from observability.stages import Stage
        assert str(Stage.EMBEDDING) == "embedding"
        assert str(Stage.LLM_GENERATION) == "llm_generation"

    def test_stage_enum_equals_plain_string(self):
        from observability.stages import Stage
        assert Stage.EMBEDDING == "embedding"

    def test_coerce_stage_accepts_stage_enum(self):
        from observability.stages import Stage, coerce_stage
        assert coerce_stage(Stage.EMBEDDING) is Stage.EMBEDDING

    def test_coerce_stage_accepts_plain_string(self):
        from observability.stages import Stage, coerce_stage
        assert coerce_stage("embedding") is Stage.EMBEDDING

    def test_coerce_stage_raises_on_unknown_string(self):
        from observability.stages import coerce_stage
        import pytest
        with pytest.raises(ValueError):
            coerce_stage("totally_unknown_stage")


class TestLatencyStoreRecord:
    """LatencyStore.record() guards and storage."""

    def _store(self):
        from observability.latency_store import LatencyStore
        return LatencyStore()

    def test_negative_latency_ignored(self):
        store = self._store()
        store.record("embedding", -10.0)
        snap = store.snapshot()
        assert snap["embedding"]["count"] == 0

    def test_nan_latency_ignored(self):
        store = self._store()
        store.record("embedding", math.nan)
        snap = store.snapshot()
        assert snap["embedding"]["count"] == 0

    def test_valid_latency_stored(self):
        store = self._store()
        store.record("embedding", 50.0)
        snap = store.snapshot()
        assert snap["embedding"]["count"] == 1

    def test_multiple_records_accumulated(self):
        store = self._store()
        for v in [10.0, 20.0, 30.0]:
            store.record("embedding", v)
        assert store.snapshot()["embedding"]["count"] == 3

    def test_record_accepts_stage_enum(self):
        from observability.stages import Stage
        store = self._store()
        store.record(Stage.LLM_GENERATION, 100.0)
        assert store.snapshot()["llm_generation"]["count"] == 1


class TestLatencyStoreSnapshot:
    """LatencyStore.snapshot() stats correctness."""

    def _store(self):
        from observability.latency_store import LatencyStore
        return LatencyStore()

    def test_all_stages_present_in_snapshot(self):
        from observability.stages import ALL_STAGES
        store = self._store()
        snap = store.snapshot()
        for stage in ALL_STAGES:
            assert str(stage) in snap

    def test_zero_sample_count_is_zero(self):
        store = self._store()
        assert store.snapshot()["embedding"]["count"] == 0

    def test_zero_sample_percentiles_are_none(self):
        store = self._store()
        stats = store.snapshot()["embedding"]
        assert stats["p50_ms"] is None
        assert stats["p95_ms"] is None
        assert stats["p99_ms"] is None

    def test_single_sample_all_percentiles_equal_value(self):
        store = self._store()
        store.record("embedding", 42.0)
        stats = store.snapshot()["embedding"]
        assert stats["p50_ms"] == 42.0
        assert stats["p95_ms"] == 42.0
        assert stats["p99_ms"] == 42.0

    def test_percentile_ordering_p99_ge_p95_ge_p50(self):
        store = self._store()
        for v in range(1, 101):
            store.record("embedding", float(v))
        stats = store.snapshot()["embedding"]
        assert stats["p99_ms"] >= stats["p95_ms"] >= stats["p50_ms"]

    def test_min_is_minimum_value(self):
        store = self._store()
        for v in [10.0, 50.0, 100.0]:
            store.record("embedding", v)
        assert store.snapshot()["embedding"]["min_ms"] == 10.0

    def test_max_is_maximum_value(self):
        store = self._store()
        for v in [10.0, 50.0, 100.0]:
            store.record("embedding", v)
        assert store.snapshot()["embedding"]["max_ms"] == 100.0

    def test_mean_is_correct(self):
        store = self._store()
        for v in [10.0, 20.0, 30.0]:
            store.record("embedding", v)
        stats = store.snapshot()["embedding"]
        assert abs(stats["mean_ms"] - 20.0) < 0.01


class TestBottleneckIdentification:
    """The bottleneck is the stage with the highest p95 latency."""

    def _store_with_data(self) -> "LatencyStore":
        from observability.latency_store import LatencyStore
        store = LatencyStore()
        # LLM generation is clearly the slowest
        for _ in range(20):
            store.record("embedding", 5.0)
            store.record("vector_retrieval", 10.0)
            store.record("llm_generation", 500.0)
            store.record("reranking", 20.0)
        return store

    def test_bottleneck_stage_has_highest_p95(self):
        store = self._store_with_data()
        snap = store.snapshot()
        stages_with_data = {
            stage: stats["p95_ms"]
            for stage, stats in snap.items()
            if stats["p95_ms"] is not None
        }
        bottleneck = max(stages_with_data, key=stages_with_data.__getitem__)
        assert bottleneck == "llm_generation"

    def test_reset_clears_all_stage_buffers(self):
        store = self._store_with_data()
        store.reset()
        snap = store.snapshot()
        for stats in snap.values():
            assert stats["count"] == 0

    def test_reset_is_idempotent(self):
        from observability.latency_store import LatencyStore
        store = LatencyStore()
        store.reset()
        store.reset()
        snap = store.snapshot()
        assert all(s["count"] == 0 for s in snap.values())

    def test_thread_safety_concurrent_records(self):
        from observability.latency_store import LatencyStore
        store = LatencyStore()
        n_threads = 20
        n_records_each = 50

        def record_many():
            for _ in range(n_records_each):
                store.record("llm_generation", 100.0)

        threads = [threading.Thread(target=record_many) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        count = store.snapshot()["llm_generation"]["count"]
        assert count == n_threads * n_records_each
