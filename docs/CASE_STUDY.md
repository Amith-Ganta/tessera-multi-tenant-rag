# Tessera — Project Case Study

## Scope

Production-style project. Exercises the concerns of a real system (rate
limiting, circuit breakers, bounded worker pools, merge-blocking eval
gate in CI, ADRs, container packaging) without having carried a real
user workload or a real operational consequence. Named deliberately:
not a controlled demo, not a real deployment — production concerns in
a single-operator environment.

---

## 1. Problem and Constraint

**Responsible for:** answering natural-language questions over a
multi-tenant document corpus with grounding guarantees. Every answer
must carry a cited source. When nothing relevant is retrieved, the
system must refuse.

**Must not do:**
- Leak documents across tenants
- Ship answers below the faithfulness threshold
- Run unbounded evaluation work on the request path
- Silently truncate retrieved context

**What "wrong" means here:** a confident answer without a supporting
citation is worse than a refusal. That single definition drove the
runtime judge loop, the merge-blocking eval gate, tenant-scoped caching,
and the bounded worker pool.

---

## 2. Architecture Decision and Tradeoff

### Decision: Move evaluation judging to a Redis-backed worker pool

**Context.** DeepEval judging (~8.5 metric calls per request) ran
in-process via `asyncio.create_task()`. Under 20 sequential async eval
requests on the live server:

- D1: `submit_judge` returned in 0.030 ms (fire-and-forget confirmed)
- D2: max 6 concurrent judges observed (unbounded)
- D3: single async eval request returned in 3,991 ms
- D4: single sync eval request returned in 19,297 ms

Unbounded concurrency meant background judges competed with the request
path for GIL, LLM API quota, and connection pool. Jobs were also lost
on pod restart — a caller received a `trace_id` that would never
resolve.

**Decision.** Move judge execution to a separate process consuming from a
Redis LPUSH/BRPOP queue with a bounded 3-worker pool
(`JUDGE_WORKER_CONCURRENCY = 3`). Cap queue depth at 100;
overflow returns HTTP 503 with `Retry-After: 60`.
Results stored in Redis under `judge:result:<trace_id>` with a 24-hour TTL.

**Tradeoff.** We chose Redis queue because background judges were
starving the request path under load. The cost: new infrastructure
dependency, second process to operate, user-visible 503 under overload.
That trade was accepted — visible failure beats invisible latency
degradation.

**Measured after.** D3 stays at 3,991 ms (request returns as soon as the
answer is generated). Judge is off the request path. Queue depth
observable on `/metrics/latency`.

**Not measured.** Post-fix D2 under live traffic. Architecturally bounded
to 3 by worker config; not observed under sustained load. Documented as
open item.

See: `docs/adr/ADR-007.md`, `docs/adr/ADR-008.md`

### Additional recorded decisions

- ADR-007: Redis message queue for judge evaluation jobs
- ADR-008: Rate limiting, circuit breaker, and bulkhead patterns
- ADR-009: Redis checkpointer for stateless A2A agents
- ADR-010: Autoscaling strategy — HPA on queue depth for judge workers

---

## 3. Evaluation Evidence

**What was tested.** Hand-authored golden set of 12 question-answer
pairs. DeepEval with five metrics: answer relevancy, correctness (GEval),
faithfulness, context precision, and context recall.

**Correctness definition.** Correct means the cited paragraph actually
supports the answer. Not string equality. A grounded claim.

**Unit coverage.** 129 tests covering the request path, pattern wiring
(circuit breaker, bulkhead, rate limiter, checkpointer, queue),
conditional refinement, semantic cache, streaming, and observability.
The count is verified by running the full suite with `uv run python -m pytest tests/ -q`; partial runs or runs outside the `uv` environment will report fewer tests and are not the authoritative count.

**Measured results (Phase 3 — 20 queries, `run_eval=False`):**

| Stage | p50 (ms) | p95 (ms) |
|---|---|---|
| embedding | 215.9 | 1005.9 |
| vector_retrieval | 4.0 | 34.8 |
| reranking | 0.004 | 0.005 (conditional skip) |
| llm_generation | 2943.4 | 3965.2 |
| post_processing | 0.007 | 0.008 |

- Client round-trip mean: **6,044 ms** (down from 10,318 ms in Phase 1)
- Retrieval p50: **4 ms** (down from 3,696 ms)
- LLM generation p50: **2,943 ms** (unchanged; model inference floor)
- Cost per question: **$0.00025**

**CI eval gate results (12 golden questions, gpt-4o-mini judge):**

| Metric | Mean | Pass rate | Threshold |
|---|---|---|---|
| Answer relevancy | 0.917 | 11/12 | 0.7 per case |
| Correctness (GEval) | 0.786 | 11/12 | 0.5 per case |
| Faithfulness | 0.889 | 8/12 counted | 0.7 per case |
| Context precision | 1.000 | 12/12 | 0.7 per case |
| Context recall | 1.000 | 12/12 | 0.7 per case |

**Does NOT establish:** behaviour on real user query distribution,
multilingual robustness, adversarial inputs beyond 5 hand-written probes,
1M requests/day scale, post-fix judge concurrency under live load.

---

## 4. Failure and Recovery

**Failure.** Phase 1 instrumentation reported `vector_retrieval` p50 =
3,696 ms, p95 = 6,125 ms — 7–18× expected for a local index over a small
corpus.

**Detected by.** Phase 1 per-stage instrumentation. Without it, this
would have looked like normal LLM slowness.

**Root cause.** Two compounding defects:

1. `get_vectorstore()` reconstructed `OpenAIEmbeddings` and `Chroma` on
   every call. No caching.
2. On the adaptive path, it was called twice per request — once via
   `retrieve_hybrid`, once via `_top1_similarity` — doubling embedding
   round trips and client constructions inside a single
   `vector_retrieval` timer.

**The change.** Cached the vectorstore per tenant index directory with a
`threading.Lock`. Modified `retrieve_hybrid` to return dense scores on
the first pass, eliminating the second retrieval call.

**The result.**

| Stage | Before (p50) | After (p50) | Delta |
|---|---|---|---|
| vector_retrieval | 3,696 ms | 4 ms | −3,692 ms (~900×) |
| client round-trip | 10,318 ms | 6,044 ms | −4,274 ms |

**Residual risk unresolved.**

- Post-fix judge concurrency under sustained load (not observed)
- Docker Compose stack end-to-end with all four patterns live
- Provider rate limits at 10× peak: DeepEval metric calls reach
  ~588/min against a 500 RPM OpenAI limit

---

## 5. Operational Readiness and Reflection

**Capacity model summary (from `docs/capacity-model.md`).**

At 10K req/day, 3× peak (0.347 RPS):

| Component | Status |
|---|---|
| API pods | 1 pod sufficient |
| LLM calls/min | 20.8 (safe, limit 500) |
| DeepEval metric calls/min | 176.9 (safe, limit 500) |
| Judge queue | Growing: +17.6 jobs/min until 503 fires |

At 1M req/day, 3× peak (34.72 RPS) — all provider rate limits exceeded;
655 judge worker replicas needed to drain queue.

**If Tessera kept running.**

| Concern | Current | Needed |
|---|---|---|
| Queue persistence | Redis in container | Volume + AOF/RDB |
| Worker scaling | Fixed 3-pool | HPA on queue depth (manifests written, not deployed) |
| Secrets | .env file | Secret manager + rotation |
| Per-tenant observability | Aggregate metrics | Per-tenant latency/cost breakdown |
| Rollback | Revert image tag | Restore image + model + prompt bundle together |
| Ingestion access control | Not implemented | Auth on upload path |
| PII audit | Not performed | Review logs and retention |
| Circuit-breaker state | Per-pod (in-process) | Redis-backed shared state (noted in ADR-008) |

**Rollback.** Restoring image tag alone is insufficient. Earlier
behaviour depends on image + model version + prompt bundle together.

**Before real workload, needs:** ingestion access control, PII audit,
per-tenant observability, regression run on every prompt or model change.

**Reflection.** The strongest improvement came from instrumentation, not
optimisation. Per-stage measurement turned an invisible 3.7-second defect
into a four-millisecond fix. The same discipline applies to any agentic
system: instrument before tuning, measure against a baseline, document
what was not measured.

---

## 6. Security Audit — Phase 2 Fixes

A targeted security review found and fixed ten vulnerabilities. All changes
are covered by regression tests (129 tests pass in the full suite).

**Critical fixes:**

| ID | Finding | Fix |
|----|---------|-----|
| SEC-01 | Token expiry not enforced — tokens were valid indefinitely | `_verify_token` now checks `time.time() - issued_at > TOKEN_MAX_AGE_SECONDS` |
| SEC-02 | `/budget` endpoint unauthenticated | Added `Depends(get_current_user)` |
| SEC-03 | Rate limiter singleton recreated per request | Moved `_rate_limiter` to module level |
| SEC-04 | Rate limiter INCR/EXPIRE race condition | Replaced two separate calls with atomic Redis pipeline |
| SEC-05 | Semantic cache key missing tenant ID — cross-tenant leak | `make_key()` now prepends tenant as first element |
| SEC-06 | Circuit breaker docstring falsely claimed Redis-backed shared state | Corrected: state is in-process only, independent per replica |
| SEC-07 | `deepseek-chat` used as default despite cost 7× higher | Changed default to `deepseek-flash` across all three config locations |
| SEC-08 | Judge queue had no retry limit or dead-letter queue | Added `MAX_JOB_ATTEMPTS=3`, `requeue_or_dlq()`, DLQ with envelope metadata |

**Test coverage added:**

- `TestSemanticCacheTenantIsolation` (4 tests): tenant A cache key never matches tenant B
- `TestTokenExpiry` (7 tests): expiry enforced, tamper-resistant, boundary-exact
- `TestDLQ` (3 tests): retry counter, DLQ routing at exhaustion, depth measurement
- `TestBlockingPop` additions (2 tests): `_attempts` incremented on first pop and on retry

**Evidence:** `uv run python -m pytest tests/ -q` → 129 passed, 0 failures.

---

## Phase 8 — Dependency Failure Analysis and Targeted Fixes (MM-01, MM-02, MM-03)

A comprehensive dependency failure analysis (`docs/DEPENDENCY_FAILURE_MATRIX.md`) was
performed across all eight runtime dependency categories. Three mismatches (MM-01 to
MM-03) were identified between claimed and actual failure behaviour.

### MM-01 — Embedding Resilience (Fixed)

**Problem:** Any transient OpenAI embedding API failure caused HTTP 500 with no fallback.
Dense retrieval and the full answer pipeline were completely blocked.

**Fix:**
- `src/rag/embedding_resilience.py`: new `EmbeddingUnavailable` exception;
  `embed_with_resilience()` wraps `embed_query` in the existing `CircuitBreaker` and
  `main_bulkhead`; raises `EmbeddingUnavailable` when the circuit is open.
- `src/rag/retriever_dense.py`: uses `embed_with_resilience()`.
- `src/rag/retriever_hybrid.py`: catches `EmbeddingUnavailable`, falls back to sparse-only
  BM25 retrieval with score `0.0` for dense arm.
- `src/api/app.py`: catches `EmbeddingUnavailable` and returns HTTP 503 with
  `{"error": "embedding provider unavailable"}` and `Retry-After: 30` header.

**Tests added:** `tests/test_embedding_resilience.py` — 6 regression tests.

### MM-02 — Honest Eval Status on Queue Publish Failure (Fixed)

**Problem:** When `judge_queue.publish()` returned `False` (Redis unavailable), the
`/ask` response still set `eval={"status": "pending"}`. The caller had no way to know
the job was never queued; polling `GET /eval/{trace_id}` would time out indefinitely.

**Fix:**
- `src/judge/async_runner.py`: `submit_judge()` now returns a status dict:
  `{"status": "pending", "trace_id": ...}` on success, or
  `{"status": "unavailable", "reason": "queue_unavailable"}` on publish failure.
  Logs a WARNING on failure.
- `src/api/app.py`: both `/ask` call sites (SSE and non-SSE) use the returned status
  dict as the `eval` field in `AskResponse` instead of a hardcoded pending dict.

**Tests added:** `tests/test_queue_publish_honesty.py` — 6 regression tests.

### MM-03 — Circuit Breaker State Scope (Accepted Limitation, ADR-014)

**Decision:** Circuit breaker state remains in-process only (not shared across replicas).
Option A (Redis-backed shared state) was rejected because it would couple the circuit
breaker to the very class of dependency it protects against, adding a new failure mode.

A startup WARNING log (`src/api/app.py`) and formal ADR (`docs/adr/ADR-014.md`) document
the limitation for operators. Upgrade path to Option A is described in the ADR.

### Updated Test Count

**Evidence:** `uv run python -m pytest tests/ -q`

| Stage | Tests |
|---|---|
| Pre-existing (Phases 1–7) | 129 |
| Dependency failure regression (Phase 8 initial) | 11 |
| MM-01 embedding resilience | 6 |
| MM-02 queue publish honesty | 6 |
| **Total** | **152** |

---

## Phase 9 — Data Lifecycle and Deletion Propagation (GAP-01, 02, 03, 04, 07, 09)

A systematic audit of data storage and deletion paths (`docs/DATA_LIFECYCLE.md`) identified
ten lifecycle gaps across the six storage subsystems (disk, Chroma, semantic cache, Redis
judge results, checkpoints, user DB). Six gaps were closed in this phase.

### GAP-01 + GAP-02 — DELETE /documents/{filename}

**Problem:** No endpoint existed to delete a single document. Tenants could not remove
uploaded files without re-uploading the entire corpus.

**Fix:**
- `DELETE /documents/{filename}` endpoint in `src/api/app.py` with:
  - Path traversal validation: rejects `..`, `/`, `\`, null bytes, leading `.` → HTTP 400
  - File existence check → HTTP 404 if not found
  - `target.unlink()` removes the file from the tenant corpus directory
  - `SemanticCache.invalidate_by_document(tenant, filename)` (GAP-02 closed)
  - `JudgeQueue.invalidate_judge_results_for_document(tenant, filename)` (GAP-03 closed)
  - Full `build_tenant_index(tenant)` rebuild from remaining corpus files
- `SemanticCache.invalidate_by_document()` added to `src/cache/semantic_cache.py`

**Tests added:** `tests/test_document_delete.py` — 7 tests covering traversal rejection
(6 parametrized bad names), null byte handling, 404 on missing file, 204 + file removal,
index rebuild, cache eviction, and idempotent second delete.

### GAP-09 — Tenant-Scoped Judge Result Keys

**Problem:** Redis judge result keys had no tenant component (`judge:result:<trace_id>`),
making tenant-level enumeration and purge impossible without a full scan.

**Fix:**
- Redis keys are now `judge:result:<tenant>:<trace_id>` in `src/judge/redis_queue.py`
- `GET /eval/{trace_id}` derives tenant from the authenticated user and calls
  `judge_queue.get_result(trace_id, tenant=tenant)` — prevents cross-tenant result reads
- `invalidate_by_tenant(tenant)` and `invalidate_judge_results_for_document(tenant, filename)`
  added to `JudgeQueue`

**Tests added:** `tests/test_redis_queue.py` — 6 new tests in `TestTenantScopedResults`.

### GAP-03 — Judge Result Invalidation on Document Deletion

**Problem:** Deleting a document left orphaned judge results in Redis until TTL expiry.
Results referencing deleted content could still be returned via `GET /eval/{trace_id}`.

**Fix:** `JudgeQueue.invalidate_judge_results_for_document(tenant, filename)` SCAN-filters
Redis keys for `judge:result:<tenant>:*`, loads each result JSON, checks the `contexts`/
`sources` field for source paths matching the deleted filename, and deletes matches.

### GAP-04 + GAP-07 — DELETE /tenant (Full GDPR-Compliant Removal)

**Problem:** No API endpoint for full tenant data removal. Deleting a tenant required
manual filesystem and database operations across six subsystems. Conversation checkpoints
were not included in any deletion path (GAP-07).

**Fix:**
- `DELETE /tenant` in `src/api/app.py` removes in order:
  1. Raw documents: `shutil.rmtree(corpus_dir)`
  2. Chroma vector index: `shutil.rmtree(index_dir)`
  3. Semantic cache: `SemanticCache.invalidate_by_tenant(tenant)`
  4. Judge results: `JudgeQueue.invalidate_by_tenant(tenant)` (when `JUDGE_QUEUE_ENABLED`)
  5. SQLite checkpoints: `SQLiteCheckpointer().delete_tenant_checkpoints(tenant)` using
     `json_extract(state_json, '$.tenant_slug') = ?` (GAP-07 closed)
  6. Redis checkpoints: `RedisCheckpointer().delete_tenant_checkpoints(tenant)` using
     SCAN + JSON body filter (GAP-07 closed)
  7. User row: `auth.delete_user(user_id)`
- `delete_user(user_id)` added to `src/auth/auth.py`
- `delete_tenant_checkpoints(tenant)` added to both `SQLiteCheckpointer`
  (`src/rag/checkpointer.py`) and `RedisCheckpointer` (`src/state/redis_checkpointer.py`)
- Endpoint is idempotent: `rmtree(ignore_errors=True)`, all cleanup functions safe on empty input

**Tests added:** `tests/test_tenant_delete.py` — 5 tests: corpus + index removal, SQLite
checkpoint deletion (isolated from other tenants), Redis checkpoint deletion (fakeredis),
judge result deletion, and idempotent second call.

### xfail → passing: test_deletion_consistency.py

Two `@pytest.mark.xfail` tests in `tests/test_deletion_consistency.py` were converted to
passing tests:

- **Test 4** (`test_single_document_deletion_removes_it_from_index`): Now explicitly
  removes the file and triggers a full rebuild, asserting the deleted document's source
  path no longer appears in Chroma query results.
- **Test 5** (`test_cache_invalidated_after_document_deletion`): Now seeds the
  `SemanticCache` with source-tagged entries and calls `invalidate_by_document()` directly,
  asserting eviction count = 1 and that an unrelated entry survives.

### Deferred Gaps

GAP-05 (analytics log unbounded growth), GAP-06 (DLQ accumulation), GAP-08 (per-document
Chroma `delete()` — full rebuild is correct, per-doc delete is an optimisation), and
GAP-10 (`clear_analytics()` not exposed via API) remain documented as future enhancements.

### Updated Test Count

| Stage | Tests |
|---|---|
| Pre-existing (Phases 1–8) | 152 |
| GAP-01 document delete (+ traversal, cache, rebuild) | 7 |
| GAP-04 tenant delete (+ checkpoints, judge results) | 5 |
| GAP-09 tenant-scoped Redis results | 6 |
| xfail converted to passing | 2 (already counted in prior phases) |
| **Total** | **170** |

---

## Phase 10 — Per-Tenant Resource Governance (3B: ADR-016)

To enforce multi-tenant fairness across shared infrastructure, Tessera now applies
three Redis-backed per-tenant limiters on every `/ask` request.  All three counters
are fail-closed: a Redis error is treated as a limit breach, preventing a degraded
Redis cluster from bypassing tenant fairness boundaries.

### Governance Axes

| Axis | Redis key | Default limit | HTTP status on breach |
|---|---|---|---|
| Daily token budget | `tokens:<tenant>:<YYYYMMDD>` | 2 000 000 tokens/day | 429 |
| Concurrent request limit | `concurrent:<tenant>` | 5 in-flight requests | 503 |
| Daily judge quota | `judge_quota:<tenant>:<YYYYMMDD>` | 200 judge calls/day | eval skipped |

The `TenantGovernor` class (`src/resilience/tenant_governance.py`) exposes three
methods — `check_token_budget`, `acquire_concurrent`/`release_concurrent`, and
`check_judge_quota` — wired at three points in the `/ask` handler.  When the judge
quota is exceeded the judge call is silently skipped and `eval` is set to
`{"status": "quota_exceeded", "reason": "tenant_judge_quota"}` rather than
returning an error to the caller.

All limits are overridable via environment variables (`TENANT_DAILY_TOKEN_BUDGET`,
`TENANT_MAX_CONCURRENT`, `TENANT_DAILY_JUDGE_QUOTA`).  ADR-016 documents the
fail-closed rationale and the design trade-offs against a fail-open approach.

### Tests Added

`tests/test_tenant_governance.py` — 8 tests (fakeredis, no real Redis required):
within-budget pass, budget breach, fail-closed on Redis error, concurrent
within-limit, concurrent limit breach, release-and-reacquire cycle, within-quota
pass, and quota breach.

### Updated Test Count

| Stage | Tests |
|---|---|
| Pre-existing (Phases 1–9) | 180 |
| Phase 3B tenant governance | 8 |
| **Total** | **188** |

