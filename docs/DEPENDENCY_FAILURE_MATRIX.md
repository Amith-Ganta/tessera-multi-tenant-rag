# Tessera — Dependency Failure Matrix

All behaviour verified against source code. File:line citations point to the
version present in the repository after commit b872d80.

---

## 1. Dependencies and Failure Behaviour

### 1.1 LLM Provider (DeepSeek / OpenAI via LiteLLM)

| Question | Answer | Evidence |
|---|---|---|
| What happens on a timeout? | LiteLLM raises after `REQUEST_TIMEOUT_SECONDS` (default 30 s). With `num_retries=2` the call is retried up to 2 more times on the same model. If all retries fail, an exception propagates to the caller. The circuit breaker records each failure. | `src/rag/llm.py:80,191-203` |
| What happens on a rate-limit / 5xx? | LiteLLM retries up to `num_retries=2` times on the primary model, then falls through to the `fallbacks` list (`openai/gpt-4o-mini` when the key is available). If no fallback key is configured, the exception propagates. | `src/rag/llm.py:88,179-195,203` |
| What HTTP status does the caller receive? | `BulkheadFullError` → HTTP 503 `Retry-After: 5`. `CircuitOpenError` → HTTP 503 `Retry-After: 30`. Any other unhandled exception propagates as HTTP 500. | `src/api/app.py:693-704` |
| Is the failure counted by the circuit breaker? | Yes. `_llm_breaker.call(completion, ...)` wraps every completion call. `CircuitBreaker.call()` records failures automatically, opening the breaker after `CIRCUIT_BREAKER_FAILURE_THRESHOLD` (= 5) consecutive failures. | `src/rag/llm.py:38,203`; `src/rag/config.py:122` |
| Is the circuit breaker state shared across replicas? | **No.** In-process only (`_llm_breaker` is a module-level singleton per process). Each replica maintains independent state. | `src/rag/llm.py:38`; `src/resilience/circuit_breaker.py` |

---

### 1.2 Redis (rate limiter, judge queue, semantic cache, checkpointer)

Redis is a shared dependency for four subsystems with different degradation
profiles:

#### Rate limiter (`src/resilience/rate_limiter.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when Redis is unreachable? | **Fail-open.** The limiter catches the exception, logs a warning, and returns `0` (treated as "count = 0, request allowed"). No `RateLimitError` is raised. | `src/resilience/rate_limiter.py:63-65,79-81` |
| Consequence of fail-open | Under a Redis outage all tenant rate limits are bypassed. A tenant can flood the API without any throttling. | (see above) |

#### Judge queue (`src/judge/redis_queue.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when Redis is unavailable at publish time? | `publish()` logs an error and returns `False`. The judge job is silently dropped — no retry, no DLQ entry. Eval result stays `{"status": "pending", ...}` indefinitely. | `src/judge/redis_queue.py:91-100` |
| What happens when Redis fails during `blocking_pop`? | Returns `None`. The worker loop receives nothing and sleeps until the next poll interval. | `src/judge/redis_queue.py:167-169` |
| What happens when a DLQ push fails while Redis is down? | Logs the error and silently drops the DLQ entry. The job is lost with no trace. | `src/judge/redis_queue.py:196-204` |
| Retry / DLQ behaviour (normal path) | `requeue_or_dlq()` re-queues failed jobs up to `MAX_JOB_ATTEMPTS` (= 3). After that, the job is pushed to `judge:queue:dlq` with a failure reason and timestamp. | `src/judge/redis_queue.py:35,171-204` |

#### Semantic cache (`src/cache/semantic_cache.py`)

| Question | Answer | Evidence |
|---|---|---|
| Is the cache backed by Redis? | **No.** The semantic cache is an in-process dict with TTL eviction. It does not depend on Redis. A process restart empties it. | `src/cache/semantic_cache.py:27-34` |
| What is the TTL? | `CACHE_TTL_SECONDS` = 21 600 s (6 h), with a cleanup thread that runs every 300 s. | `src/rag/config.py:105`; `src/cache/semantic_cache.py:27,97-99` |
| What fails when the cache is unavailable? | Nothing fails. The cache is checked after retrieval; a miss simply proceeds to the LLM. | `src/api/app.py:683-685` |

#### Redis checkpointer (`src/state/redis_checkpointer.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when Redis is unavailable for `save_state`? | Logs a warning and returns without raising. The A2A supervisor's state for that step is not persisted. | `src/state/redis_checkpointer.py:59-61` |
| What happens when Redis is unavailable for `load_state`? | Returns `None`. The supervisor starts a fresh workflow rather than resuming a prior one. | `src/state/redis_checkpointer.py:69-71` |
| Default backend | `CHECKPOINTER_BACKEND` defaults to `"sqlite"`. Redis checkpointer is only active when `CHECKPOINTER_BACKEND=redis`. | `src/rag/config.py:130` |

---

### 1.3 OpenAI Embeddings (`src/rag/retriever_dense.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when the OpenAI embedding API is unavailable? | `embed_query()` raises (network error or API error). The exception propagates through `_embed_query` → `retrieve` / `retrieve_with_scores` → strategy → `/ask`, resulting in HTTP 500. There is no retry or fallback for the embedding step. | `src/rag/retriever_dense.py:100-101,106-108` |
| Is there an embedding fallback? | **No.** LiteLLM fallback only covers the generation call in `complete()`. Embedding failure is unmitigated. | `src/rag/llm.py:88` — fallback chain is generation-only |
| What is the vectorstore failure mode? | If `_build_vectorstore()` fails (bad `OPENAI_API_KEY`, index directory missing), the exception propagates from `get_vectorstore()` on first call. Subsequent calls to `get_vectorstore()` will retry construction because the cache key is never stored on error. | `src/rag/retriever_dense.py:52-78` |

---

### 1.4 Cross-encoder Reranker (`src/rag/reranker.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when the model fails to load? | `_get_model()` raises. Called at startup via `_warm_reranker()` in app startup handler. Startup catches the exception (`pass`) so startup does not fail. On first real request, `rerank()` raises again and the exception propagates as HTTP 500. | `src/api/app.py:186-192`; `src/rag/reranker.py:14-16` |
| Is the reranker always called? | **No.** Conditional on `RERANK_SKIP_THRESHOLD` (= 0.90). If the top-1 dense score exceeds 0.90, reranking is skipped entirely. | `src/rag/config.py:78` |
| What is the model loaded? | `cross-encoder/ms-marco-MiniLM-L-6-v2` from HuggingFace. Loaded via `sentence_transformers`. A missing `sentence_transformers` package or blocked HuggingFace download fails the import and raises at startup. | `src/rag/reranker.py:11,16` |

---

### 1.5 Bulkhead (`src/resilience/bulkhead.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when the semaphore is full? | `BulkheadFullError` is raised immediately (non-blocking acquire). Caught by `/ask` handler → HTTP 503 `Retry-After: 5`. | `src/api/app.py:693-698` |
| Pool sizes | `MAIN_POOL_SIZE` = 10 (LLM calls), `JUDGE_POOL_SIZE` = 3 (eval). Both from `config.py`. | `src/rag/config.py:126-127` |
| Is the bulkhead shared across replicas? | **No.** In-process semaphore per replica. | `src/resilience/bulkhead.py:94-95` |

---

### 1.6 Circuit Breaker (`src/resilience/circuit_breaker.py`)

| Question | Answer | Evidence |
|---|---|---|
| When does it open? | After `CIRCUIT_BREAKER_FAILURE_THRESHOLD` (= 5) consecutive failures on the same breaker instance. | `src/rag/config.py:122` |
| Recovery window | OPEN → HALF_OPEN after `CIRCUIT_BREAKER_RECOVERY_SECONDS` (= 30 s). A successful HALF_OPEN call closes it. | `src/rag/config.py:123` |
| What does the caller receive when OPEN? | `CircuitOpenError` propagates from `_llm_breaker.call(...)` → caught by `/ask` → HTTP 503 `Retry-After: 30`. | `src/api/app.py:699-704` |
| Is state shared across replicas? | **No.** In-process only. The documentation in `docs/adr/ADR-008.md` and `docs/CASE_STUDY.md` note this as a known limitation. | `src/rag/llm.py:38` |

---

### 1.7 A2A Agent Endpoints (Drafter / Judge HTTP)

| Question | Answer | Evidence |
|---|---|---|
| What happens when the Drafter HTTP endpoint is unreachable? | `A2AJsonRpcClient.send_message()` raises (requests exception). `_call_drafter()` catches the exception, records a `drafter_fallback` transcript entry, falls back to `_call_drafter_in_process()`, and sets `self.mode = "in_process"` for subsequent calls in this request. | `src/orchestrator/a2a_supervisor.py:193-213` |
| What happens when the Judge HTTP endpoint is unreachable? | `_call_judge()` catches the exception and returns `{"score": {}, "passed": None, "feedback": None, "enabled": False, "error": "..."}`. A `passed=None` result **never gates** — the supervisor accepts the draft and marks it "not verified". | `src/orchestrator/a2a_supervisor.py:219-231` |
| A2A timeout | `A2A_TIMEOUT_SECONDS` = 120 s (default, from `TESSERA_A2A_TIMEOUT_SECONDS`). | `src/orchestrator/a2a_supervisor.py:36` |
| What happens after `MAX_RETRIES` judge failures? | After `MAX_RETRIES` (= 2) refine attempts without a pass, the supervisor sets `guard_passed = False`, adds a note "gate NOT met after N attempts", and returns the best-effort answer flagged unverified. | `src/orchestrator/a2a_supervisor.py:349-354` |

---

### 1.8 LangSmith Tracing (`src/rag/observability.py`)

| Question | Answer | Evidence |
|---|---|---|
| What happens when LangSmith is unreachable or not configured? | **No-op.** `_Span.__init__` catches all exceptions in `create_run` and sets `self._run = None`. `finish()` returns immediately when `_run is None`. Nothing in the request path blocks on LangSmith. | `src/rag/observability.py:40-53,55-62` |
| Is tracing enabled by default? | **No.** Requires `LANGSMITH_API_KEY` or `LANGCHAIN_API_KEY` to be set. Disabled also if `LANGSMITH_TRACING=false`. | `src/rag/observability.py:19-22` |

---

## 2. Summary Matrix

| Dependency | Failure Mode | Degradation | HTTP to caller | Mitigated? |
|---|---|---|---|---|
| LLM provider (primary) | Timeout / 5xx | LiteLLM retries ×2, then falls back to `gpt-4o-mini` | 503 (bulkhead/circuit) or 500 | Yes — LiteLLM fallback + circuit breaker |
| LLM provider (all) | Budget cap exceeded | `RuntimeError` on next call | 500 | Yes — hard cap |
| OpenAI Embeddings | Network / API failure | Exception propagates, no retry | 500 | **No** — unmitigated |
| Chroma vectorstore | Index missing / build failure | Exception on first call | 500 | Partially — warm-up at startup |
| Cross-encoder reranker | Load failure | Warm-up suppressed; 500 on first use | 500 | Partially — conditional skip (score > 0.90) |
| Redis — rate limiter | Unreachable | **Fail-open** — all tenants bypass limit | 200 (unthrottled) | Partially — known, logged |
| Redis — judge queue publish | Unreachable | Job dropped silently; eval stuck "pending" | 200 (no error surfaced) | **No** — loss is silent |
| Redis — judge queue DLQ | Unreachable during DLQ push | DLQ entry lost; no trace | N/A | **No** |
| Redis — checkpointer | Unreachable | Fresh workflow on every request; mid-flight state lost | 200 (degraded) | Partially — returns None, logged |
| Bulkhead full | Semaphore exhausted | Reject immediately | 503 `Retry-After: 5` | Yes |
| Circuit breaker OPEN | 5 consecutive LLM failures | All LLM calls blocked for 30 s | 503 `Retry-After: 30` | Yes |
| A2A Drafter unreachable | `requests` exception | Automatic in-process fallback | 200 (degraded) | Yes |
| A2A Judge unreachable | `requests` exception | Answer returned unverified (`passed=None`) | 200 (unverified) | Partially — never gates |
| LangSmith | Network error / missing key | No-op; tracing silently disabled | 200 (no trace) | Yes — fully no-op |

---

## 3. Mismatches

The following documented or asserted behaviours are **absent or incomplete** in the code:

| ID | Claimed | Actual | Location |
|---|---|---|---|
| MM-01 | Embedding failures have a fallback | No embedding fallback exists. `_FALLBACK_CHAIN` covers generation only. | `src/rag/llm.py:88`; `src/rag/retriever_dense.py` |
| MM-02 | Judge-queue publish failure is surfaced to the caller | `publish()` returns `False` but the `/ask` handler does not check the return value or emit a warning in the response. Eval stays `{"status": "pending"}` indefinitely with no indication the job was dropped. | `src/judge/redis_queue.py:88-100`; `src/api/app.py:733` |
| MM-03 | Circuit-breaker state is shared / consistent under multi-replica deploy | State is in-process only. Three replicas maintain three independent counters. One replica can be open while others are closed. | `src/rag/llm.py:38`; noted in `docs/CASE_STUDY.md` |

---

## 4. Top Three Unmitigated Failures

Ranked by impact on query correctness and silent failure risk:

1. **OpenAI Embedding failure (MM-01)** — Any network error to the OpenAI embedding
   endpoint turns every `/ask` request into an HTTP 500 with no fallback, no retry,
   and no graceful degradation. Retrieval and the entire answer pipeline are blocked.

2. **Judge-queue publish drop silently (MM-02)** — When Redis is unavailable at
   publish time, `publish()` drops the job and returns `False`. The `/ask` response
   still sets `eval={"status": "pending", ...}`. The caller has no way to know the
   job was never queued. Polling `GET /eval/{trace_id}` will time out indefinitely.

3. **Rate-limiter fail-open (no alerting) (partial MM-03 impact)** — When Redis is
   down, all tenant rate limits are bypassed without any indication to the operator
   or caller that enforcement is off. A sustained Redis outage gives every tenant
   unlimited throughput.
