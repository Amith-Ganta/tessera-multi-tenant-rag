# Tessera Load Test: Concurrency Fix Comparison

**Fix applied**: Multi-worker uvicorn (4 workers) + async PBKDF2 offloaded to ThreadPoolExecutor  
**Commit**: fa59978  
**Date**: 2026-09-20

---

## Pre-fix Baseline (single-worker uvicorn, sync PBKDF2)

### 50 Users (5 min)

| Endpoint     | Requests | Failures | Failure % | p50     | p95     | Avg     |
|--------------|----------|----------|-----------|---------|---------|---------|
| POST /ask    | 66       | 52       | 78.8%     | 120,000 | 121,000 | 100,925 |
| POST /auth/login | 50   | 0        | 0%        | 66,000  | 71,000  | 55,899  |
| **Aggregated** | **166** | **101** | **60.8%** | **64,000** | **121,000** | **58,716** |

RPS (aggregated): 0.76

### 100 Users (5 min)

| Endpoint     | Requests | Failures | Failure % | p50     | p95     | Avg     |
|--------------|----------|----------|-----------|---------|---------|---------|
| POST /ask    | 52       | 48       | 92.3%     | 120,000 | 120,000 | 99,336  |
| POST /auth/login | 76   | 8        | 10.5%     | 41,000  | 236,000 | 98,401  |
| **Aggregated** | **228** | **156** | **68.4%** | **36,000** | **207,000** | **67,954** |

RPS (aggregated): 0.92

---

## Post-fix Results (4-worker uvicorn, async PBKDF2 + ThreadPoolExecutor)

### 50 Users (5 min)

| Endpoint     | Requests | Failures | Failure % | p50     | p95     | Avg     |
|--------------|----------|----------|-----------|---------|---------|---------|
| POST /ask    | 120      | 24       | 20.0%     | 60,000  | 102,000 | 54,417  |
| POST /auth/login | 50   | 10       | 20.0%     | 22,000  | 64,000  | 27,533  |
| POST /auth/signup | 50  | 49       | 98%*      | 930     | 9,500   | 3,259   |
| **Aggregated** | **220** | **83**  | **37.7%** | **33,000** | **88,000** | **36,680** |

RPS (aggregated): 0.90  
*Signup failures are expected 400s — user already exists.

### 100 Users (5 min)

| Endpoint     | Requests | Failures | Failure % | p50     | p95     | Avg     |
|--------------|----------|----------|-----------|---------|---------|---------|
| POST /ask    | 284      | 103      | 36.3%     | 53,000  | 120,000 | 57,856  |
| POST /auth/login | 100  | 0        | 0%        | 40,000  | 105,000 | 43,303  |
| POST /auth/signup | 100 | 100      | 100%*     | 23,000  | 55,000  | 22,877  |
| **Aggregated** | **484** | **203** | **41.9%** | **41,000** | **120,000** | **47,622** |

RPS (aggregated): 1.63  
*All 100 signup failures are expected 400s — single shared test account.

---

## Delta Summary

| Metric                  | Pre-fix 50u | Post-fix 50u | Pre-fix 100u | Post-fix 100u |
|-------------------------|-------------|--------------|--------------|----------------|
| /ask failure rate       | 78.8%       | **20.0%**    | 92.3%        | **36.3%**      |
| /ask p50 latency        | 120,000 ms  | **60,000 ms**| 120,000 ms   | **53,000 ms**  |
| /ask p95 latency        | 121,000 ms  | **102,000 ms**| 120,000 ms  | **120,000 ms** |
| Aggregated RPS          | 0.76        | **0.90**     | 0.92         | **1.63**       |
| Aggregated failure rate | 60.8%       | **37.7%**    | 68.4%        | **41.9%**      |
| /auth/login p50         | 66,000 ms   | **22,000 ms**| 41,000 ms    | **40,000 ms**  |

---

## Bottleneck Analysis

**Root cause of pre-fix failures**: Single uvicorn worker serialised all requests through one Python process. PBKDF2 (200,000 iterations, ~0.3 s/hash) ran synchronously on the async event loop, blocking every other coroutine while computing each login hash. With 50+ concurrent users each triggering PBKDF2 on `on_start`, the event loop queue depth grew unbounded and requests hit the 120-second timeout ceiling.

**What the fix improved**:
- 4 uvicorn workers distribute OS-level concurrency across 4 independent Python processes, removing the single-process bottleneck.
- PBKDF2 is offloaded to a dedicated `ThreadPoolExecutor(max_workers=4)`, keeping the async event loop free during hash computation.
- `/ask` failure rate dropped from 78.8% → 20.0% at 50u and 92.3% → 36.3% at 100u.
- `/ask` p50 latency halved at 50u (120 s → 60 s); `/auth/login` p50 dropped from 66 s to 22 s at 50u.
- Aggregated throughput at 100u increased 77% (0.92 → 1.63 RPS).

**Remaining bottleneck**: At 100u, `/ask` p95 still hits the 120 s timeout ceiling. The dominant failure modes are `ReadTimeout` (33 occurrences, LLM call latency) and `RemoteDisconnected` (70 occurrences, likely worker memory pressure). The OpenAI GPT-4o-mini calls are the new limiting factor — each `/ask` blocks a worker thread for 5–30 s on the LLM round-trip. Mitigation options: streaming responses, request queuing with a job queue, or horizontal scaling (more replicas / larger instance).

---

## Test Environment

- API: `http://127.0.0.1:8000` (Docker Compose, `api` service)
- Workers: 4 uvicorn processes (1 master + 4 workers)
- Redis: Docker Compose `redis` service
- Judge worker: Docker Compose `judge-worker` service
- Locust: v2.46.6, `between(0.5, 2.0)` wait time, `/ask` timeout=120s
- Ramp rate: 5 users/s
- Duration: 5 min per run
