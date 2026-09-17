# Tessera Capacity Model

Measured baseline (Phase 4 warm live-server):

| Measurement | Value |
|---|---|
| p95 async-eval RTT (D3) | 3,991 ms |
| p95 sync-eval RTT (D4) | 19,297 ms |
| submit_judge overhead (D1) | 0.030 ms |
| Max concurrent judges, 20 reqs (D2) | 6 |
| DeepEval metrics per judge job | 8.5 (avg) |
| Observed per-metric judge latency | ~6,700 ms |
| Total judge job duration | ~57,000 ms (8.5 × 6,700) |

---

## 1. RPS calculations — 10,000 requests/day

```
Average RPS  = 10,000 / 86,400 = 0.116 RPS
Peak RPS 3×  = 0.116 × 3      = 0.347 RPS
Peak RPS 10× = 0.116 × 10     = 1.157 RPS
```

## 2. Fan-out per request (async-eval path)

Each `/ask` with `run_eval=True` and `JUDGE_MODE=async` triggers:

| Sub-call | Count |
|---|---|
| Embedding API call | 1 |
| Vector DB query (Chroma) | 1 |
| Reranker inference (conditional) | 0–1 |
| LLM generation (DeepSeek) | 1 |
| Judge job published to queue | 1 |
| DeepEval metric calls (per judge job) | ~8.5 |
| SQL queries (auth log + analytics + session) | 3 |
| Cache lookup | 1 |

**Derived load at peak 10× (1.157 RPS):**

| Component | Calls/sec |
|---|---|
| Embedding API | 1.157 |
| Chroma vector query | 1.157 |
| Reranker | ≤1.157 |
| LLM (DeepSeek) | 1.157 |
| Judge jobs enqueued | 1.157 |
| DeepEval metric calls | 9.8 |
| SQL queries | 3.47 |
| Cache lookups | 1.157 |

## 3. Queue dynamics — 10K/day

```
Judge worker concurrency = 3 (Phase 5 cap)
Judge job duration       = 57,000 ms = 57 s
Judge throughput         = 3 / 57    = 0.053 jobs/sec

At peak 3× arrival rate  = 0.347 jobs/sec
At peak 10× arrival rate = 1.157 jobs/sec

Queue growth (3×)  = 0.347 – 0.053 = +0.294 jobs/sec = +17.6 jobs/min
Queue growth (10×) = 1.157 – 0.053 = +1.104 jobs/sec = +66.2 jobs/min
```

At 10K/day the queue is **always growing during any sustained peak**. The
`JUDGE_QUEUE_MAX_DEPTH = 100` cap means the queue fills in ~1.5 minutes at
10× peak and then the 503 shed-load policy activates.

## 4. Provider rate limits — 10K/day

Assumptions: OpenAI 500 RPM, DeepSeek 500 RPM per key (conservative default).

```
Peak 10× RPS = 1.157 RPS

LLM calls/min       = 1.157 × 60 = 69.4   (limit: 500 → safe)
DeepEval calls/min  = 9.8   × 60 = 588    (limit: 500 → EXCEEDED at 10×)
Embedding calls/min = 1.157 × 60 = 69.4   (limit: 500 → safe)
```

**DeepEval metric calls hit the 500 RPM OpenAI limit at 10× peak of 10K/day.**
Above ~8.3 judge-job-triggering requests/minute (~500 RPS / 8.5 / 60),
the judge pool receives 429s from OpenAI and backs off.

At 3× peak (0.347 RPS × 8.5 × 60 = 176.9 calls/min) — still safe.

## 5. Horizontal scale — 10K/day

```
Single process p95 RTT (async-eval)  = 3.991 s
Process concurrency (FastAPI/asyncio) = ~100 concurrent connections
Pods needed at peak 3×  = ceil(0.347 × 3.991 / 100) = 1 pod
Pods needed at peak 10× = ceil(1.157 × 3.991 / 100) = 1 pod
```

At 10K/day a single FastAPI pod handles all traffic. The bottleneck is **not**
the API process — it is the judge queue drain rate and provider rate limits.

---

## 6. RPS calculations — 1,000,000 requests/day

```
Average RPS  = 1,000,000 / 86,400 = 11.57 RPS
Peak RPS 3×  = 11.57 × 3          = 34.72 RPS
Peak RPS 10× = 11.57 × 10         = 115.7 RPS
```

## 7. Fan-out — 1M/day at peak 10× (115.7 RPS)

| Component | Calls/sec |
|---|---|
| Embedding API | 115.7 |
| Chroma vector query | 115.7 |
| LLM (DeepSeek) | 115.7 |
| Judge jobs enqueued | 115.7 |
| DeepEval metric calls | 983 |
| SQL queries | 347 |

## 8. Queue dynamics — 1M/day

```
Judge throughput = 0.053 jobs/sec (3 workers, 57s per job)
Arrival rate 3×  = 34.72 jobs/sec
Arrival rate 10× = 115.7 jobs/sec

Queue growth (3×)  = 34.72  – 0.053 = +34.67 jobs/sec = +2,080 jobs/min
Queue growth (10×) = 115.7  – 0.053 = +115.6 jobs/sec = +6,940 jobs/min
```

The 100-depth queue cap is hit in under 3 seconds at 1M/day peak. 503s dominate.
To drain the queue, workers must scale: 34.72 / 0.053 = **655 workers** at 3×.

## 9. Provider rate limits — 1M/day

```
DeepEval calls/min at 3×  = 34.72 × 8.5 × 60 = 17,687  (limit 500 → 35× over)
LLM calls/min at 3×       = 34.72 × 60        = 2,083   (limit 500 → 4× over)
Embedding calls/min at 3× = 34.72 × 60        = 2,083   (limit 500 → 4× over)
```

All three providers are rate-limited at 1M/day. Mitigation requires:
- Multiple API keys (key rotation / per-tenant keys)
- LiteLLM retry + fallback chains (already in place)
- Async judge path to avoid blocking /ask on judge 429s

## 10. Horizontal scale — 1M/day

```
p95 RTT = 3.991 s
Pods at 3×  = ceil(34.72 × 3.991 / 100) = ceil(13.86) = 14 pods
Pods at 10× = ceil(115.7 × 3.991 / 100) = ceil(46.19) = 47 pods
```

---

## Bottleneck Analysis

| Scale | First saturating component | Why |
|---|---|---|
| 10K/day, 3× peak | Judge queue drain | 0.053 jobs/sec throughput vs 0.347 arrival |
| 10K/day, 10× peak | OpenAI RPM (DeepEval) | 588 metric calls/min vs 500 limit |
| 1M/day, 3× peak | All provider rate limits; API pods (14 needed) | Judge queue fills in <3 s |
| 1M/day, 10× peak | API pods (47 needed) + provider limits (35× over) | Entire stack overwhelmed |

## What Breaks at 10×

1. **Judge queue** fills to `JUDGE_QUEUE_MAX_DEPTH=100` in ~86 seconds at 10K/day.
   The 503 shed-load response fires, protecting the API process.
2. **OpenAI RPM** is exceeded for DeepEval metric calls (~588/min vs 500 limit).
   The judge worker receives 429s and retries with exponential back-off.
3. **Circuit breaker** on the judge path trips after 5 consecutive 429 failures,
   opening for 30 seconds and letting the provider recover.

## The Bottleneck Sentence

**At 1.157 RPS (10× peak of 10K/day), the judge queue drain rate (0.053 jobs/sec)
becomes the bottleneck because 3 workers processing 57-second jobs cannot keep up
with the arrival rate, causing unbounded queue growth until the 503 capacity gate activates.**
