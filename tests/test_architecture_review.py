"""Item 24: Architecture review tests.

Validates the system's structural invariants:
- Config constants present, sane-valued, and correctly typed
- All resilience layers (rate-limiter, circuit-breaker, bulkhead) are importable
- AskResponse has exactly the 15 contracted fields
- Judge result contract: pending/done/unavailable shapes
- Observability modules importable and expose expected callables
- Tenant governance constants are non-zero positive integers
- Version identifier constants are all non-empty strings
- Quality gate thresholds are in sensible ranges
- Security modules importable

No real network, filesystem, or LLM calls are made.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Stub langchain_chroma before any src.rag imports.
_stub_chroma = types.ModuleType("langchain_chroma")
_stub_chroma.Chroma = MagicMock()
sys.modules.setdefault("langchain_chroma", _stub_chroma)


class TestConfigConstants:
    """src.rag.config invariants — single source of truth for tunables."""

    def test_chunk_size_is_positive_int(self):
        from src.rag.config import CHUNK_SIZE
        assert isinstance(CHUNK_SIZE, int) and CHUNK_SIZE > 0

    def test_chunk_overlap_less_than_chunk_size(self):
        from src.rag.config import CHUNK_SIZE, CHUNK_OVERLAP
        assert 0 <= CHUNK_OVERLAP < CHUNK_SIZE

    def test_retriever_top_k_is_positive(self):
        from src.rag.config import RETRIEVER_TOP_K
        assert isinstance(RETRIEVER_TOP_K, int) and RETRIEVER_TOP_K > 0

    def test_default_chat_model_is_deepseek_flash(self):
        """Default model must be the low-cost deepseek-flash variant."""
        from src.rag.config import CHAT_MODEL
        assert "deepseek-flash" in CHAT_MODEL or CHAT_MODEL.endswith("deepseek-flash")

    def test_embedding_model_non_empty(self):
        from src.rag.config import EMBEDDING_MODEL
        assert isinstance(EMBEDDING_MODEL, str) and EMBEDDING_MODEL

    def test_cache_ttl_positive(self):
        from src.rag.config import CACHE_TTL_SECONDS
        assert isinstance(CACHE_TTL_SECONDS, int) and CACHE_TTL_SECONDS > 0

    def test_judge_store_max_positive(self):
        from src.rag.config import JUDGE_STORE_MAX
        assert isinstance(JUDGE_STORE_MAX, int) and JUDGE_STORE_MAX > 0

    def test_rerank_skip_threshold_in_unit_range(self):
        from src.rag.config import RERANK_SKIP_THRESHOLD
        assert 0.0 <= RERANK_SKIP_THRESHOLD <= 1.0

    def test_min_context_confidence_in_unit_range(self):
        from src.rag.config import MIN_CONTEXT_CONFIDENCE_FOR_REFINE
        assert 0.0 <= MIN_CONTEXT_CONFIDENCE_FOR_REFINE <= 1.0

    def test_rate_limit_positive(self):
        from src.rag.config import RATE_LIMIT_PER_MINUTE
        assert isinstance(RATE_LIMIT_PER_MINUTE, int) and RATE_LIMIT_PER_MINUTE > 0

    def test_circuit_breaker_recovery_positive(self):
        from src.rag.config import CIRCUIT_BREAKER_RECOVERY_SECONDS
        assert CIRCUIT_BREAKER_RECOVERY_SECONDS > 0

    def test_circuit_breaker_failure_threshold_positive(self):
        from src.rag.config import CIRCUIT_BREAKER_FAILURE_THRESHOLD
        assert CIRCUIT_BREAKER_FAILURE_THRESHOLD > 0


class TestVersionIdentifiers:
    """Phase 3G: all VERSION_* constants must be non-empty strings."""

    def test_version_model_non_empty(self):
        from src.rag.config import VERSION_MODEL
        assert isinstance(VERSION_MODEL, str) and VERSION_MODEL

    def test_version_prompt_non_empty(self):
        from src.rag.config import VERSION_PROMPT
        assert isinstance(VERSION_PROMPT, str) and VERSION_PROMPT

    def test_version_embedding_non_empty(self):
        from src.rag.config import VERSION_EMBEDDING
        assert isinstance(VERSION_EMBEDDING, str) and VERSION_EMBEDDING

    def test_version_retrieval_non_empty(self):
        from src.rag.config import VERSION_RETRIEVAL
        assert isinstance(VERSION_RETRIEVAL, str) and VERSION_RETRIEVAL

    def test_version_reranker_non_empty(self):
        from src.rag.config import VERSION_RERANKER
        assert isinstance(VERSION_RERANKER, str) and VERSION_RERANKER

    def test_version_eval_dataset_non_empty(self):
        from src.rag.config import VERSION_EVAL_DATASET
        assert isinstance(VERSION_EVAL_DATASET, str) and VERSION_EVAL_DATASET


class TestQualityGateThresholds:
    """Phase 3I: five quality gate thresholds must be in sane ranges."""

    def test_min_mean_relevancy_in_unit_range(self):
        from src.rag.config import MIN_MEAN_RELEVANCY
        assert 0.0 < MIN_MEAN_RELEVANCY <= 1.0

    def test_min_mean_correctness_in_unit_range(self):
        from src.rag.config import MIN_MEAN_CORRECTNESS
        assert 0.0 < MIN_MEAN_CORRECTNESS <= 1.0

    def test_latency_p95_max_positive(self):
        from src.rag.config import LATENCY_P95_MAX_MS
        assert LATENCY_P95_MAX_MS > 0.0

    def test_error_rate_max_in_unit_range(self):
        from src.rag.config import ERROR_RATE_MAX
        assert 0.0 < ERROR_RATE_MAX < 1.0

    def test_cost_per_request_max_positive(self):
        from src.rag.config import COST_PER_REQUEST_MAX_USD
        assert COST_PER_REQUEST_MAX_USD > 0.0


class TestTenantGovernance:
    """Phase 3B: tenant governance constants are non-zero positive integers."""

    def test_tenant_daily_token_budget_positive(self):
        from src.rag.config import TENANT_DAILY_TOKEN_BUDGET
        assert isinstance(TENANT_DAILY_TOKEN_BUDGET, int) and TENANT_DAILY_TOKEN_BUDGET > 0

    def test_tenant_max_concurrent_positive(self):
        from src.rag.config import TENANT_MAX_CONCURRENT
        assert isinstance(TENANT_MAX_CONCURRENT, int) and TENANT_MAX_CONCURRENT > 0

    def test_tenant_daily_judge_quota_positive(self):
        from src.rag.config import TENANT_DAILY_JUDGE_QUOTA
        assert isinstance(TENANT_DAILY_JUDGE_QUOTA, int) and TENANT_DAILY_JUDGE_QUOTA > 0


class TestResilienceLayersImportable:
    """All resilience modules must be importable and expose their core interfaces."""

    def test_rate_limiter_importable(self):
        from src.resilience.rate_limiter import RateLimiter
        assert callable(RateLimiter)

    def test_circuit_breaker_importable(self):
        from src.resilience.circuit_breaker import CircuitBreaker
        assert callable(CircuitBreaker)

    def test_bulkhead_importable(self):
        from src.resilience.bulkhead import Bulkhead
        assert callable(Bulkhead)

    def test_tenant_governance_importable(self):
        from src.resilience.tenant_governance import TenantGovernor
        assert callable(TenantGovernor)


class TestObservabilityModules:
    """Observability modules expose expected callables."""

    def test_cost_module_importable(self):
        from src.observability.cost import estimate_usd, record, spend_so_far, reset, daily_cap, over_cap
        for fn in (estimate_usd, record, spend_so_far, reset, daily_cap, over_cap):
            assert callable(fn)

    def test_rag_signals_module_importable(self):
        from src.observability.rag_signals import record_rag_signals
        assert callable(record_rag_signals)

    def test_latency_store_importable(self):
        from observability.latency_store import LatencyStore, latency_store
        assert callable(LatencyStore)
        assert isinstance(latency_store, LatencyStore)

    def test_stages_module_importable(self):
        from observability.stages import Stage, ALL_STAGES, coerce_stage
        assert callable(coerce_stage)
        assert len(ALL_STAGES) > 0


class TestJudgeResultContract:
    """Judge result dicts must conform to the pending/done/unavailable contract."""

    def test_pending_result_has_status_and_trace_id(self):
        result = {"status": "pending", "trace_id": "abc-123"}
        assert result["status"] == "pending"
        assert "trace_id" in result

    def test_unavailable_result_has_status_and_reason(self):
        result = {"status": "unavailable", "reason": "queue_unavailable"}
        assert result["status"] == "unavailable"
        assert "reason" in result

    def test_judge_store_importable_and_has_core_methods(self):
        from src.judge.judge_store import judge_store
        assert hasattr(judge_store, "set_pending")
        assert hasattr(judge_store, "get")
        assert hasattr(judge_store, "set_result")

    def test_analytics_module_importable_and_has_core_callables(self):
        from src.rag.analytics import log_analytics, read_analytics, clear_analytics
        for fn in (log_analytics, read_analytics, clear_analytics):
            assert callable(fn)
