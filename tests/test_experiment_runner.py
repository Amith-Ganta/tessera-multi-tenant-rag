"""Tests for the retrieval experiment framework (Phase 3F).

All tests use a mock embedder and mock retrieve_fn so no real API calls
are made.
"""

from __future__ import annotations

from experiments.config import ExperimentConfig
from experiments.runner import run_experiment


def _mock_embedder(text: str) -> list[float]:
    return [0.1] * 8


def _mock_retrieve(embedding: list[float], top_k: int, metric: str) -> list[str]:
    corpus = ["doc-a", "doc-b", "doc-c", "doc-d", "doc-e"]
    return corpus[:top_k]


class TestExperimentRunner:
    def test_aggregates_are_computed_for_all_cases(self):
        config = ExperimentConfig(
            name="test-run",
            top_k=3,
            embedder_fn=_mock_embedder,
        )
        cases = [
            ("What is IAM?", ["doc-a", "doc-b"]),
            ("Explain encryption", ["doc-b"]),
        ]
        result = run_experiment(config, cases, _mock_retrieve)

        assert result["aggregates"]["count"] == 2
        assert 0.0 <= result["aggregates"]["mean_precision_at_k"] <= 1.0
        assert 0.0 <= result["aggregates"]["mean_recall_at_k"] <= 1.0
        assert result["aggregates"]["mean_latency_ms"] >= 0.0

    def test_config_is_not_mutated_and_embedder_fn_excluded_from_output(self):
        config = ExperimentConfig(
            name="immutability-check",
            top_k=2,
            embedder_fn=_mock_embedder,
        )
        cases = [("query", ["doc-a"])]
        result = run_experiment(config, cases, _mock_retrieve)

        # embedder_fn must NOT appear in the serialised config dict
        assert "embedder_fn" not in result["config"]
        # original config object is unchanged
        assert config.name == "immutability-check"
        assert config.top_k == 2
        assert config.embedder_fn is _mock_embedder
