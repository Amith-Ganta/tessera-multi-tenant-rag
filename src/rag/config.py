"""Single source of configuration for the Project 2 RAG baseline."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Load this project's own .env when present. Otherwise fall back to the sibling
# Project 1 .env, which holds the shared keys this portfolio reuses. Neither file
# is committed; only the resolved path is referenced here, never any key value.
_SHARED_ENV_FILE = PROJECT_ROOT.parent / "AI-JOB-Search-Project-1" / ".env"
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE)
elif _SHARED_ENV_FILE.exists():
    load_dotenv(dotenv_path=_SHARED_ENV_FILE)


def get_openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            f"OPENAI_API_KEY is not set. Expected it in the local .env file at {ENV_FILE}."
        )
    return key


def get_deepseek_api_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError(
            f"DEEPSEEK_API_KEY is not set. Expected it in the local .env file at {ENV_FILE}."
        )
    return key


def get_tavily_api_key() -> str:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise RuntimeError(
            f"TAVILY_API_KEY is not set. Expected it in the local .env file at {ENV_FILE}."
        )
    return key


CORPUS_DIR = PROJECT_ROOT / "data" / "corpus"
INDEX_DIR = PROJECT_ROOT / "data" / "index" / "chroma"
GOLDENS_PATH = PROJECT_ROOT / "goldens" / "retriever_goldens.json"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# OpenAI provides embeddings; CHAT_MODEL selects the generation provider.
# Override via CHAT_MODEL env var (e.g. set in docker-compose.yml).
EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = os.environ.get("CHAT_MODEL", "deepseek/deepseek-flash")
RETRIEVER_TOP_K = 5


def get_chat_api_key() -> str:
    """Return the API key appropriate for the active CHAT_MODEL."""
    if CHAT_MODEL.startswith("deepseek"):
        return get_deepseek_api_key()
    return get_openai_api_key()

# Phase 2 Part A -- conditional re-ranking.
# When the top-1 retrieved chunk is already a very strong match, the cross-encoder
# re-ranker rarely changes the order but still costs a full model pass over every
# candidate. 0.90 (on Chroma's normalised [0, 1] relevance score) marks "retrieval
# is already confident": above it we trust the fused order and skip the re-ranker;
# at or below it we still pay for re-ranking, because that is exactly the regime
# where re-ordering earns its keep. The gate is strictly-above only, so 0.90 itself
# still runs the re-ranker (a conservative default -- err towards re-ranking).
RERANK_SKIP_THRESHOLD = 0.90

# Phase 2 Part B -- conditional refinement.
# The generate-judge-refine loop only helps when there is solid context to refine
# against. If the best retrieved chunk is weak, refining a low-faithfulness answer
# just polishes a guess: the judge's feedback pushes the model to sound more
# confident about context that never supported the claim. 0.50 is the floor of
# "context worth refining against" (again on Chroma's [0, 1] relevance score);
# below it we return an honest insufficient-context answer instead of refining.
MIN_CONTEXT_CONFIDENCE_FOR_REFINE = 0.50

# Phase 4a -- async judging.
# "async" runs the judge in a background asyncio task and returns immediately with
# eval={"status": "pending", "trace_id": ...}; callers poll GET /eval/{trace_id}.
# "sync" is the Phase 3 behaviour: judge blocks the /ask response (used by CI).
JUDGE_MODE = "async"  # "sync" restores Phase 3 blocking behaviour for CI

# Maximum number of trace_id → result entries to keep in the in-process judge store.
# Once the limit is reached, the oldest entry is evicted (ring-buffer semantics).
JUDGE_STORE_MAX = 5000

# Phase 4b -- semantic caching.
# When enabled, a cache key (sha256 of query + sorted chunk IDs + model name) is
# checked before the LLM call. A hit returns the stored answer instantly.
# In async mode the cache is written only after the judge task completes and passes.
# In sync mode the cache is written only if both guard and judge passed.
CACHE_ENABLED = True
CACHE_TTL_SECONDS = 21600  # 6 hours

# Phase 5 — Redis judge queue (Pattern 4: Message Queue).
# When JUDGE_QUEUE_ENABLED=True the FastAPI process publishes judge jobs to
# Redis instead of spawning in-process asyncio tasks.  A separate judge_worker.py
# process drains the queue with a bounded pool (JUDGE_WORKER_CONCURRENCY).
JUDGE_QUEUE_ENABLED = True
JUDGE_QUEUE_NAME = "judge:queue"
JUDGE_RESULTS_PREFIX = "judge:result:"
JUDGE_QUEUE_MAX_DEPTH = 100
JUDGE_WORKER_CONCURRENCY = 3
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Phase 5 — Rate limiting (Pattern 2).
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "100"))

# Phase 5 — Circuit breaker (Pattern 5).
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_RECOVERY_SECONDS = 30

# Phase 5 — Bulkhead connection pools (Pattern 5).
MAIN_POOL_SIZE = 10
JUDGE_POOL_SIZE = 3

# Phase 5 — Redis checkpointer (Pattern 6: Statelessness).
CHECKPOINTER_BACKEND = os.environ.get("CHECKPOINTER_BACKEND", "sqlite")  # "sqlite" | "redis"
CHECKPOINTER_TTL_SECONDS = 86400  # 24 hours

# Phase 8 — Canary deployment (ADR-011).
# Requests are hashed by tenant_id; if hash % 100 < MODEL_CANARY_PERCENT,
# the canary model version is used instead of the default. Set to 0 to disable.
MODEL_CANARY_PERCENT = int(os.environ.get("MODEL_CANARY_PERCENT", "0"))
MODEL_CANARY_VERSION = os.environ.get("MODEL_CANARY_VERSION", "gpt-4o-2024-11-20")
