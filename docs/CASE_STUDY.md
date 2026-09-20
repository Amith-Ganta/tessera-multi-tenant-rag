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

**Unit coverage.** 104 tests covering the request path, pattern wiring
(circuit breaker, bulkhead, rate limiter, checkpointer, queue),
conditional refinement, semantic cache, streaming, and observability.

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
are covered by regression tests (53 tests pass).

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

**Evidence:** `pytest tests/ -q` → 53 passed, 0 failures.
