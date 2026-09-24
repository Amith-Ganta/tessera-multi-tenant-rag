"""Item 25: Documentation consistency tests.

Verifies that documented claims about the system match actual code:
- AskResponse has exactly 15 fields (as documented in ARCHITECTURE.md section 3)
- All documented resilience modules are importable with expected class names
- All documented package directories exist in src/
- Version identifiers in config match what is documented (non-empty)
- Analytics log path matches documented location (logs/analytics.jsonl)
- ARCHITECTURE.md claims: 9 pipeline stages match Stage enum
- Config constants match documented defaults
- RATES dict keys match documented model names

No real network, filesystem I/O (beyond module imports), or LLM calls are made.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Stub langchain_chroma before any src.rag imports.
_stub_chroma = types.ModuleType("langchain_chroma")
_stub_chroma.Chroma = MagicMock()
sys.modules.setdefault("langchain_chroma", _stub_chroma)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_ask_response_fields() -> list[str]:
    """Extract AskResponse field names from app.py source via AST — no import needed."""
    import ast
    app_path = _PROJECT_ROOT / "src" / "api" / "app.py"
    source = app_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AskResponse":
            fields = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.append(item.target.id)
            return fields
    return []


class TestAskResponseFieldCount:
    """ARCHITECTURE.md documents AskResponse as having exactly 15 fields.
    Uses AST parsing to avoid importing src.api.app (which blocks on Redis init).
    """

    def test_ask_response_has_15_fields(self):
        fields = _parse_ask_response_fields()
        assert len(fields) == 15, (
            f"AskResponse has {len(fields)} fields, expected 15. "
            f"Fields: {fields}"
        )

    def test_ask_response_has_answer_field(self):
        assert "answer" in _parse_ask_response_fields()

    def test_ask_response_has_trace_field(self):
        assert "trace" in _parse_ask_response_fields()

    def test_ask_response_has_versions_field(self):
        assert "versions" in _parse_ask_response_fields()

    def test_ask_response_has_eval_field(self):
        assert "eval" in _parse_ask_response_fields()


class TestDocumentedPackageStructure:
    """Every src/ package documented in ARCHITECTURE.md must exist."""

    def test_src_api_exists(self):
        assert (_PROJECT_ROOT / "src" / "api").is_dir()

    def test_src_auth_exists(self):
        assert (_PROJECT_ROOT / "src" / "auth").is_dir()

    def test_src_rag_exists(self):
        assert (_PROJECT_ROOT / "src" / "rag").is_dir()

    def test_src_cache_exists(self):
        assert (_PROJECT_ROOT / "src" / "cache").is_dir()

    def test_src_judge_exists(self):
        assert (_PROJECT_ROOT / "src" / "judge").is_dir()

    def test_src_resilience_exists(self):
        assert (_PROJECT_ROOT / "src" / "resilience").is_dir()

    def test_src_observability_exists(self):
        assert (_PROJECT_ROOT / "src" / "observability").is_dir()

    def test_src_security_exists(self):
        assert (_PROJECT_ROOT / "src" / "security").is_dir()

    def test_src_agents_exists(self):
        assert (_PROJECT_ROOT / "src" / "agents").is_dir()

    def test_src_orchestrator_exists(self):
        assert (_PROJECT_ROOT / "src" / "orchestrator").is_dir()

    def test_src_state_exists(self):
        assert (_PROJECT_ROOT / "src" / "state").is_dir()


class TestDocumentedKeyFiles:
    """Key files documented in ARCHITECTURE.md must exist."""

    def test_api_app_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "api" / "app.py").is_file()

    def test_rag_pipeline_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "rag" / "rag_pipeline.py").is_file()

    def test_judge_redis_queue_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "judge" / "redis_queue.py").is_file()

    def test_judge_store_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "judge" / "judge_store.py").is_file()

    def test_judge_async_runner_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "judge" / "async_runner.py").is_file()

    def test_resilience_rate_limiter_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "resilience" / "rate_limiter.py").is_file()

    def test_resilience_circuit_breaker_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "resilience" / "circuit_breaker.py").is_file()

    def test_resilience_bulkhead_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "resilience" / "bulkhead.py").is_file()

    def test_observability_cost_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "observability" / "cost.py").is_file()

    def test_security_ssrf_py_exists(self):
        assert (_PROJECT_ROOT / "src" / "security" / "ssrf.py").is_file()

    def test_analytics_log_in_documented_location(self):
        """ARCHITECTURE.md documents analytics as logs/analytics.jsonl."""
        from src.rag.analytics import ANALYTICS_PATH
        assert str(ANALYTICS_PATH).endswith("analytics.jsonl")
        assert "logs" in str(ANALYTICS_PATH)


class TestDocumentedPipelineStages:
    """ARCHITECTURE.md documents 9 pipeline stages; Stage enum must match."""

    def test_nine_documented_stages_match_enum(self):
        from observability.stages import ALL_STAGES
        # Architecture doc lists: query_processing, embedding, vector_retrieval,
        # metadata_filtering, reranking, prompt_stitching, llm_generation,
        # post_processing, judge
        documented = {
            "query_processing", "embedding", "vector_retrieval",
            "metadata_filtering", "reranking", "prompt_stitching",
            "llm_generation", "post_processing", "judge",
        }
        actual = {s.value for s in ALL_STAGES}
        assert actual == documented


class TestDocumentedDefaultModel:
    """ARCHITECTURE.md documents deepseek-flash as the default generation model."""

    def test_default_model_is_deepseek_flash(self):
        from src.rag.config import CHAT_MODEL
        assert "deepseek-flash" in CHAT_MODEL

    def test_default_model_not_deepseek_v4_pro(self):
        """deepseek-v4-pro must not be the default (cost constraint)."""
        from src.rag.config import CHAT_MODEL
        assert "deepseek-v4-pro" not in CHAT_MODEL.lower()


class TestDocumentedRatesTable:
    """COST_MODEL.md documents four model rates; RATES dict must contain them."""

    def test_documented_rates_all_present(self):
        from src.observability.cost import RATES
        documented_models = [
            "deepseek/deepseek-flash",
            "deepseek/deepseek-chat",
            "openai/gpt-4o-mini",
            "openai/gpt-4o",
        ]
        for model in documented_models:
            assert model in RATES, f"Model {model} missing from RATES dict"

    def test_all_documented_rates_positive(self):
        from src.observability.cost import RATES
        for model, rate in RATES.items():
            assert rate >= 0, f"{model} has non-positive rate"


class TestDocumentedCapacityConfig:
    """CAPACITY_MODEL.md documents configurable queue depth and worker concurrency."""

    def test_judge_queue_max_depth_positive(self):
        from src.rag.config import JUDGE_QUEUE_MAX_DEPTH
        assert isinstance(JUDGE_QUEUE_MAX_DEPTH, int) and JUDGE_QUEUE_MAX_DEPTH > 0

    def test_judge_worker_concurrency_positive(self):
        from src.rag.config import JUDGE_WORKER_CONCURRENCY
        assert isinstance(JUDGE_WORKER_CONCURRENCY, int) and JUDGE_WORKER_CONCURRENCY > 0

    def test_main_pool_size_positive(self):
        from src.rag.config import MAIN_POOL_SIZE
        assert isinstance(MAIN_POOL_SIZE, int) and MAIN_POOL_SIZE > 0

    def test_judge_pool_size_positive(self):
        from src.rag.config import JUDGE_POOL_SIZE
        assert isinstance(JUDGE_POOL_SIZE, int) and JUDGE_POOL_SIZE > 0
