"""Item 10: Retrieval metric correctness tests.

Covers precision@k and recall@k formulas directly, boundary conditions,
and runner aggregate consistency.  No real API calls are made.
"""

from __future__ import annotations

import pytest

from experiments.config import ExperimentConfig
from experiments.runner import _precision_at_k, _recall_at_k, run_experiment


# ---------------------------------------------------------------------------
# Unit tests for _precision_at_k
# ---------------------------------------------------------------------------

class TestPrecisionAtK:
    def test_perfect_precision(self):
        retrieved = ["doc-a", "doc-b", "doc-c"]
        relevant = {"doc-a", "doc-b", "doc-c"}
        assert _precision_at_k(retrieved, relevant) == pytest.approx(1.0)

    def test_zero_precision(self):
        retrieved = ["doc-x", "doc-y"]
        relevant = {"doc-a", "doc-b"}
        assert _precision_at_k(retrieved, relevant) == pytest.approx(0.0)

    def test_partial_precision(self):
        # 2 of 4 retrieved are relevant → P@4 = 0.5
        retrieved = ["doc-a", "doc-x", "doc-b", "doc-y"]
        relevant = {"doc-a", "doc-b"}
        assert _precision_at_k(retrieved, relevant) == pytest.approx(0.5)

    def test_empty_retrieved_returns_zero(self):
        assert _precision_at_k([], {"doc-a"}) == pytest.approx(0.0)

    def test_empty_relevant_set_with_hits_gives_correct_numerator(self):
        # No relevant docs → 0 hits → P = 0/len(retrieved) = 0.0
        retrieved = ["doc-a", "doc-b"]
        assert _precision_at_k(retrieved, set()) == pytest.approx(0.0)

    def test_single_hit_among_many(self):
        retrieved = ["doc-a", "doc-b", "doc-c", "doc-d", "doc-e"]
        relevant = {"doc-c"}
        assert _precision_at_k(retrieved, relevant) == pytest.approx(0.2)

    def test_duplicates_in_retrieved_count_once_each(self):
        # "doc-a" appears twice; each occurrence counts independently
        retrieved = ["doc-a", "doc-a"]
        relevant = {"doc-a"}
        # 2 hits out of 2 retrieved
        assert _precision_at_k(retrieved, relevant) == pytest.approx(1.0)

    def test_top_k_equals_one(self):
        assert _precision_at_k(["doc-a"], {"doc-a"}) == pytest.approx(1.0)
        assert _precision_at_k(["doc-z"], {"doc-a"}) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Unit tests for _recall_at_k
# ---------------------------------------------------------------------------

class TestRecallAtK:
    def test_perfect_recall(self):
        retrieved = ["doc-a", "doc-b"]
        relevant = {"doc-a", "doc-b"}
        assert _recall_at_k(retrieved, relevant) == pytest.approx(1.0)

    def test_zero_recall(self):
        retrieved = ["doc-x"]
        relevant = {"doc-a", "doc-b"}
        assert _recall_at_k(retrieved, relevant) == pytest.approx(0.0)

    def test_partial_recall(self):
        # 1 of 3 relevant retrieved → R@k = 1/3
        retrieved = ["doc-a", "doc-x"]
        relevant = {"doc-a", "doc-b", "doc-c"}
        assert _recall_at_k(retrieved, relevant) == pytest.approx(1 / 3)

    def test_empty_relevant_set_returns_one(self):
        # No relevant documents → recall is defined as 1.0 (nothing to recall)
        assert _recall_at_k(["doc-a"], set()) == pytest.approx(1.0)

    def test_empty_retrieved_returns_zero(self):
        assert _recall_at_k([], {"doc-a"}) == pytest.approx(0.0)

    def test_large_relevant_small_retrieved(self):
        # 1 of 10 relevant docs retrieved → R = 0.1
        retrieved = ["doc-1"]
        relevant = {f"doc-{i}" for i in range(1, 11)}
        assert _recall_at_k(retrieved, relevant) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Integration: runner aggregates
# ---------------------------------------------------------------------------

def _fixed_embedder(text: str) -> list[float]:
    return [0.5] * 4


class TestRunnerAggregates:
    def _make_config(self, top_k: int = 3) -> ExperimentConfig:
        return ExperimentConfig(name="metric-test", top_k=top_k, embedder_fn=_fixed_embedder)

    def _make_retrieve(self, corpus: list[str]):
        def retrieve(embedding, top_k, metric):
            return corpus[:top_k]
        return retrieve

    def test_perfect_retrieval_gives_all_ones(self):
        # corpus == expected for both queries so P=1 and R=1 for each
        corpus = ["doc-a", "doc-b", "doc-c"]
        cases = [
            ("query-1", ["doc-a", "doc-b", "doc-c"]),
            ("query-2", ["doc-a", "doc-b", "doc-c"]),
        ]
        config = self._make_config(top_k=3)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        agg = result["aggregates"]
        assert agg["mean_precision_at_k"] == pytest.approx(1.0)
        assert agg["mean_recall_at_k"] == pytest.approx(1.0)

    def test_zero_retrieval_gives_all_zeros(self):
        corpus = ["doc-x", "doc-y", "doc-z"]
        cases = [
            ("query-1", ["doc-a", "doc-b"]),
            ("query-2", ["doc-c"]),
        ]
        config = self._make_config(top_k=3)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        agg = result["aggregates"]
        assert agg["mean_precision_at_k"] == pytest.approx(0.0)
        assert agg["mean_recall_at_k"] == pytest.approx(0.0)

    def test_per_query_and_aggregate_counts_match(self):
        corpus = ["doc-a", "doc-b"]
        cases = [("q1", ["doc-a"]), ("q2", ["doc-b"]), ("q3", ["doc-a"])]
        config = self._make_config(top_k=2)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        assert len(result["per_query"]) == 3
        assert result["aggregates"]["count"] == 3

    def test_empty_cases_gives_zero_aggregates(self):
        config = self._make_config()
        result = run_experiment(config, [], self._make_retrieve(["doc-a"]))
        agg = result["aggregates"]
        assert agg["count"] == 0
        assert agg["mean_precision_at_k"] == pytest.approx(0.0)
        assert agg["mean_recall_at_k"] == pytest.approx(0.0)

    def test_mean_latency_is_non_negative(self):
        corpus = ["doc-a", "doc-b"]
        cases = [("q1", ["doc-a"]), ("q2", ["doc-b"])]
        config = self._make_config(top_k=2)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        assert result["aggregates"]["mean_latency_ms"] >= 0.0

    def test_aggregates_are_bounded_between_zero_and_one(self):
        corpus = ["doc-a", "doc-b", "doc-c"]
        cases = [("q", ["doc-a", "doc-x"])]
        config = self._make_config(top_k=3)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        for key in ("mean_precision_at_k", "mean_recall_at_k"):
            val = result["aggregates"][key]
            assert 0.0 <= val <= 1.0, f"{key}={val} out of [0,1]"

    def test_per_query_results_contain_required_keys(self):
        corpus = ["doc-a"]
        cases = [("q", ["doc-a"])]
        config = self._make_config(top_k=1)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        pq = result["per_query"][0]
        required = {"query", "expected", "retrieved", "precision_at_k", "recall_at_k", "latency_ms"}
        assert required <= pq.keys(), f"Missing keys: {required - pq.keys()}"

    def test_top_k_one_limits_retrieved_to_one(self):
        corpus = ["doc-a", "doc-b", "doc-c"]
        cases = [("q", ["doc-a"])]
        config = self._make_config(top_k=1)
        result = run_experiment(config, cases, self._make_retrieve(corpus))
        assert len(result["per_query"][0]["retrieved"]) == 1


# ---------------------------------------------------------------------------
# Metric identity: P and R coincide when |retrieved| == |relevant|
# ---------------------------------------------------------------------------

class TestMetricIdentity:
    def test_when_retrieved_equals_relevant_size_and_perfect_hit_pK_equals_rK(self):
        retrieved = ["a", "b"]
        relevant = {"a", "b"}
        assert _precision_at_k(retrieved, relevant) == _recall_at_k(retrieved, relevant)

    def test_pK_and_rK_are_independent_when_sizes_differ(self):
        # 1 hit out of 3 retrieved (P=1/3), 1 out of 1 relevant (R=1)
        retrieved = ["a", "b", "c"]
        relevant = {"a"}
        assert _precision_at_k(retrieved, relevant) != _recall_at_k(retrieved, relevant)
