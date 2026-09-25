# Tessera — multi-tenant RAG API

A question-answering service over per-tenant document collections. Each answer carries a
cited source, is judged by a cross-family model before it leaves the process, and will not
be returned if the evidence is too thin to support it. A change that lowers answer quality
on the golden set cannot be merged.

Python · FastAPI · LangGraph · Redis · Chroma · DeepEval · Docker

[![Tests](https://img.shields.io/badge/tests-705%20passing-green)](tests/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

705 tests passing · 22 ADRs · 3 CI jobs · 7 runbooks · 15 docs

---

## What it does

1. **Routes before it retrieves.** A greeting, an arithmetic expression, and a document
   question do not all need a vector search. A heuristic router handles the cheap cases
   locally and escalates to an LLM classifier only when it is genuinely unsure.
2. **Retrieves densely and sparsely, then fuses.** Chroma vector search and BM25 run
   against the same per-tenant index and combine via reciprocal rank fusion — embeddings
   miss exact identifiers; BM25 misses paraphrase.
3. **Re-ranks only when re-ranking can help.** The cross-encoder runs when top-1 dense
   similarity is at or below 0.90, and is skipped when retrieval is already confident.
   Measured effect: reranking p50 drops to 0.004 ms on the confident path.
4. **Judges its own answer before returning it.** A cross-family `gpt-4o-mini` judge
   scores faithfulness and relevancy; a failing answer is regenerated with the judge's
   own objection as feedback, capped at two retries.
5. **Refuses to refine against weak evidence.** If the judge fails an answer *and*
   top-1 retrieval similarity was below 0.50, the system returns an explicit
   insufficient-context message rather than a more fluent guess.
6. **Runs evaluation off the request path.** Judging goes to a Redis LPUSH/BRPOP queue
   drained by a bounded 3-worker pool. Scoring never competes with answering; the
   response returns with a `trace_id` and the client polls for the verdict.
7. **Enforces per-tenant resource fairness.** Three Redis-backed governors on every
   `/ask`: daily token budget (2M tokens), concurrent request limit (5 in-flight), and
   daily judge quota (200 calls). All fail-closed — a Redis error is treated as a breach.
8. **Blocks merges on quality regressions.** DeepEval runs 12 golden question-answer
   pairs in CI against floors on mean relevancy (0.6) and mean correctness (0.5). The
   gate runs only when an AI-affecting path changes.

## What it does NOT do

- Carry a real user workload. Every number in this repo traces to a committed file from
  a controlled evaluation or load test.
- Claim Kubernetes is deployed. `k8s/` manifests are written and reviewed; they have
  not run against a live cluster.
- Claim load-test results are harness-clean. The Locust `on_start` calls `r.json()`
  unchecked, so Locust exits non-zero even on runs that produced valid latency data.
- Handle adversarial inputs beyond five hand-written probes.
- Protect an ingest endpoint. Upload has no rate limit or content-type guard (documented
  as threat T1 in `docs/THREAT_MODEL.md`).
- Persist Redis across container restarts. No AOF or RDB is configured.

---

## Architecture

```
                          ┌─────────────────────────────────────────┐
                          │                Request path              │
  Client ──POST /ask──►  Rate limiter (Redis, 100/min/tenant)        │
                          │  Tenant governor (budget / concurrent /  │
                          │  judge quota)                            │
                          │  Router (heuristic → LLM classifier)    │
                          │  Hybrid retrieval (Chroma + BM25 + RRF) │
                          │  Conditional cross-encoder rerank        │
                          │  Circuit breaker ─► Bulkhead(10) ─► LLM │
                          │  Answer guard (judge → refine ≤2)        │
                          └─────────────┬───────────────────────────┘
                                        │  LPUSH judge:queue
                                        ▼
                          ┌─────────────────────────────────────────┐
                          │           Async judge pool              │
                          │  3 workers · BRPOP · depth cap 100      │
                          │  SETEX judge:result:<tenant>:<trace_id> │
                          │  24-hour TTL                            │
                          └─────────────────────────────────────────┘

  Redis substrate: rate counters, judge queue/results, tenant governors,
                   circuit-breaker state, A2A checkpoints
  Per-tenant isolation: separate Chroma persist dir per tenant
```

Each tenant gets its own Chroma directory. A missing filter cannot leak data — the wrong
index simply does not contain the other tenant's vectors. Isolation is structural, not
dependent on every query being written correctly.

---

## Quick start

```bash
docker compose up -d
curl http://127.0.0.1:8000/health
```

Use `127.0.0.1`, not `localhost`. On Windows, `dllhost.exe` intercepts the IPv6 loopback
before the request reaches the container.

```bash
# Sign up, log in, ask
curl -X POST http://127.0.0.1:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"YourPass123!"}'

TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"YourPass123!"}' | jq -r .token)

curl -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"What does this knowledge base cover?"}'
```

Run the test suite:

```bash
uv run python -m pytest tests/ -q
```

Run the evaluation gate:

```bash
uv run python evals/run_eval.py
```

---

## Key design decisions

Seven decisions are worth calling out; the ADRs have the full context.

**1. Judge queue via Redis LPUSH/BRPOP, not `asyncio.create_task`** (ADR-007)

In-process async judging competed with the request path for GIL, LLM quota, and
connection pool. Measured: a single async eval request took 3,991 ms; 6 concurrent judges
ran unbounded. Moving to a bounded 3-worker Redis queue keeps judging off the request
path and caps the blast radius. The cost is a second process to operate and user-visible
503 when the queue depth hits 100. That trade was accepted — visible failure beats
invisible latency degradation.

**2. Per-tenant Chroma directories, not filtered collections** (ADR-004)

A metadata filter on a shared collection works until one query omits the filter. With
separate directories, the wrong index simply does not exist. Cross-tenant leakage is
structurally excluded, not tested away.

**3. Three failure modes, three failure directions** (ADR-008, ADR-015)

Rate limiter fails open (Redis down should not take the API down). Circuit breaker fails
closed (hammering a dead provider extends its outage). Bulkhead rejects immediately
(queueing indefinitely converts a capacity problem into a timeout problem). Uniform
failure policy is a design smell.

**4. Per-tenant resource governance fails closed** (ADR-016)

Token budget, concurrent slots, and judge quota are Redis-backed. A Redis error is treated
as a limit breach, not a pass-through. The rationale: degraded Redis silently bypassing
tenant fairness is worse than a brief 429/503 under Redis instability.

**5. Cross-encoder skip threshold at strictly above 0.90** (ADR-003)

The skip requires strict inequality so ties and unknown similarities resolve toward doing
more work. The cost of a needless rerank is milliseconds; the cost of a wrongly-skipped
one is a wrong answer.

**6. Refuse to refine below 0.50 retrieval confidence**

When the judge fails an answer and top-1 similarity was below 0.50, the loop stops and
returns an explicit refusal. Refining against weak context produces answers that are more
fluent and no more true — exactly the failure mode that users cannot detect.

**7. HPA on queue depth, not CPU, for judge workers** (ADR-010)

Judge workers spend their lives blocked on network I/O at near-zero CPU utilisation.
Scaling on CPU would watch a flat line while the backlog grew. Queue depth is the signal
that tracks the actual work waiting.

---

## Technical stack

| Concern | Choice | Notes |
|---|---|---|
| API | FastAPI + uvicorn | 4 workers; PBKDF2 on `ThreadPoolExecutor` to release GIL |
| Retrieval | Chroma (dense) + BM25 (sparse) | Fused via reciprocal rank fusion |
| Reranking | `ms-marco-MiniLM-L-6-v2` | Conditional skip at top-1 > 0.90 |
| Generation | LiteLLM gateway | `deepseek/deepseek-flash` default; fallback chain to OpenAI |
| Judging | `gpt-4o-mini` (cross-family) | Off-path via Redis queue |
| Caching | In-process semantic cache | SHA-256 key = `tenant|query|chunk_ids|model` |
| Resilience | Circuit breaker + bulkhead + rate limiter | Three separate failure directions |
| Eval | DeepEval, 5 metrics | Merge-blocking CI gate, `gpt-4o-mini` judge |
| Checkpointing | SQLite (default) or Redis | A2A agent state; switchable via `CHECKPOINTER_BACKEND` |
| Observability | LangSmith tracing, analytics JSONL, `/metrics/latency` | RAG quality signals in a separate record type |

---

## Testing (honest)

```
uv run python -m pytest tests/ -q
705 passed, 8 skipped, 0 failed
```

The 705 passing tests cover: request routing, retrieval pipeline, guard loop, semantic
cache, circuit breaker, bulkhead, rate limiter, token expiry, tenant isolation, judge
queue (DLQ, retry, at-most-once), data lifecycle (document and tenant deletion), tenant
governance, embedding resilience, SSE streaming, A2A agent boundary, observability
signals, shadow promotion gate, cost model, and 74 architecture/documentation tests.

The 8 skipped tests pass individually but fail in the full suite due to a test harness
ordering issue. `test_api_startup.py::test_app_imports_without_secrets` purges
`src.api.app` from `sys.modules` and re-imports it; downstream test files that imported
`app` at collection time then hold a stale module reference. All 8 are annotated with
`@pytest.mark.skip` pointing to `docs/TEST_ISOLATION.md`, which documents the root cause
and the fix path (move the import check to a subprocess).

The CI eval gate runs 12 golden questions with DeepEval and `gpt-4o-mini` as judge.
Results at last gate run:

| Metric | Mean score | Pass rate | Gate floor |
|---|---|---|---|
| Answer relevancy | 0.917 | 11/12 | 0.6 mean |
| Correctness (GEval) | 0.786 | 11/12 | 0.5 mean |
| Context precision | 1.000 | 12/12 | 0.7 per case |
| Context recall | 1.000 | 12/12 | 0.7 per case |

One golden (`g11`) fails on routing: it was sent to the direct strategy with no context
and answered "I do not know." That is a genuine routing miss. It is left in the report
because a suite that only shows green has stopped being a measurement.

---

## Documentation map

| File | What to read it for |
|---|---|
| [`docs/CASE_STUDY.md`](docs/CASE_STUDY.md) | Problem framing, architecture decisions, all measured numbers, failure narrative |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Full system reference: package map, request lifecycle, tenant isolation layers |
| [`docs/SENIOR_ENGINEERING_REVIEW.md`](docs/SENIOR_ENGINEERING_REVIEW.md) | Security audit (10 fixes), critical mechanism review, open items |
| [`docs/TEST_ISOLATION.md`](docs/TEST_ISOLATION.md) | Root cause of the 8 skipped tests, fix path |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | STRIDE analysis, 12 open risks |
| [`docs/DISASTER_RECOVERY.md`](docs/DISASTER_RECOVERY.md) | 6 failure scenario playbooks, RTO/RPO table |
| [`docs/CAPACITY_MODEL.md`](docs/CAPACITY_MODEL.md) | Modelled throughput at 10K and 1M req/day |
| [`docs/DATA_LIFECYCLE.md`](docs/DATA_LIFECYCLE.md) | All six storage subsystems, deletion paths, gap audit |
| [`docs/adr/`](docs/adr/) | 22 architecture decision records |
| [`docs/runbooks/`](docs/runbooks/) | 7 operational runbooks (Redis failover, LLM failover, index rebuild, DLQ drain, cost cap, rolling restart, incident response) |
| [`evals/reports/`](evals/reports/) | Committed eval reports: metrics summary, tenancy isolation |
| [`loadtest/results/`](loadtest/results/) | Locust CSVs, concurrency fix comparison |

---

## Known limitations

**LLM latency is the live bottleneck at 100 concurrent users.** `/ask` p95 pins at the
120 s timeout ceiling under load. Three honest fixes exist: streaming so a worker is not
held for the full generation, a job queue for `/ask` the way the judge path already uses,
or additional replicas. None is implemented.

**Circuit-breaker state is per-pod.** State lives in-process. With four uvicorn workers
five failures must accumulate within one worker to open its breaker, so the effective
threshold across the pod is higher than the configured value. Documented in ADR-014.

**Semantic cache is per-process, not shared across replicas.** Cache hit rate degrades
with pod count. Redis-backed shared cache is the upgrade path; deferred.

**K8s manifests are not deployed.** `k8s/` contains HPA manifests and deployment specs.
The custom metrics adapter for queue-depth HPA is not wired up.

**Redis has no persistence.** Container restart loses queued judge jobs.

**Rate limiting is fixed-window.** One key per (tenant, minute) permits up to 2× the
configured limit across a window boundary. Named here rather than described as a sliding
window it is not.

**Secrets are in `.env`.** A real deployment needs a secret manager with rotation.

**8 tests are order-dependent.** Documented in `docs/TEST_ISOLATION.md`. They pass in
isolation. The fix requires refactoring `test_api_startup.py` to run the module-purge
test in a subprocess.

---

## What I would do differently

**Run the import check in a subprocess.** `test_app_imports_without_secrets` purges a
live module and re-imports it, corrupting module identity for every test that runs after
it. A `subprocess.run(["python", "-c", "from src.api.app import app"])` call gives the
same coverage with zero cross-test side effects. Ten lines of change, eliminates 8 skips.

**Instrument earlier.** The 900× retrieval regression existed from the first commit. Had
per-stage timing been part of the first test run, the fix would have cost one hour instead
of a debugging session. Per-stage instrumentation is not optimisation — it is the
prerequisite for knowing what to optimise.

**Add a load-test harness gate to CI.** The Locust file exists; it has never been run
in a reproducible environment. A headless Locust run against a Docker Compose stack in
CI would surface concurrency regressions before they reach review.

**Implement the RPOPLPUSH pattern for the judge queue.** The current LPUSH/BRPOP is
at-most-once: a worker crash after pop but before acknowledgement silently drops the job.
ADR-021 documents the upgrade path. For the judge use case the consequence is a missing
eval result — acceptable. For any path where a missing result is not acceptable, the
pattern needs to change before that path is added.

**Add a DLQ depth cap.** `JUDGE_DLQ_MAX_DEPTH` is documented as a future env var. Without
it, persistent judge failures accumulate indefinitely in Redis memory.

---

## Project layout

```
.
├── src/
│   ├── api/            # FastAPI routes, tenant resolution, budget enforcement
│   ├── auth/           # PBKDF2 user store, bearer tokens (async via ThreadPoolExecutor)
│   ├── rag/            # router, retrieval, rerank, strategies, guard, judge, checkpointer
│   ├── agents/         # A2A Drafter and Judge agent servers
│   ├── orchestrator/   # A2A supervisor with HTTP + in-process fallback
│   ├── resilience/     # rate limiter, circuit breaker, bulkhead, tenant governor
│   ├── security/       # SSRF guard, CORS config
│   ├── cache/          # semantic cache (SHA-256 keyed, TTL, tenant-scoped)
│   ├── observability/  # cost accumulator, RAG quality signals
│   └── state/          # Redis checkpointer for stateless A2A
├── tests/              # 705 passing, 8 skipped (see docs/TEST_ISOLATION.md)
├── evals/              # DeepEval harness, gate script, committed reports
├── goldens/            # 12 golden question-answer pairs
├── experiments/        # Offline retrieval experiment framework (ExperimentConfig)
├── loadtest/           # Locust file, CSVs, comparison writeup
├── docs/               # case study, capacity model, ADRs, runbooks, threat model
├── k8s/                # HPA manifests (written, not deployed)
└── .github/workflows/  # regression tests + eval gate (path-filtered)
```
