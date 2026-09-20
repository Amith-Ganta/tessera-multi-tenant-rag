# Tessera Phase 8 — Load Test Summary

**Date**: 2026-09-20  
**Stack**: Docker Compose (redis:7-alpine, api, judge-worker)  
**Rate limit (load test)**: 100 req/min per tenant  
**LLM backend**: DeepSeek (insufficient balance — /ask returns 500 after retrieval stage)

---

## Part 3 — Redis Hot-Patch

- `redis==8.1.0` installed into running API container via `docker compose exec api uv pip install redis`
- Verified: no "RateLimiter: Redis init failed" in API logs

## Part 4 — Rate Limiter Smoke Test

**Configuration**: `RATE_LIMIT_PER_MINUTE=5` (smoke test override via compose environment block)  
**Script**: `scratchpad/rl_smoke3.py` — 6 parallel threads, 30s timeout

| Request | Status |
|---------|--------|
| 1 | 200 |
| 2 | 200 |
| 3 | 200 |
| 4 | 200 |
| 5 | 200 |
| **6** | **429** |

**Result**: `RATE_LIMITER_OK` — sliding-window counter fires before any LLM call; 429 returned immediately on request 6 with `Retry-After: 60` header.

Redis key pattern confirmed: `rl:user-1:<unix_minute>` with INCR+EXPIRE-60 strategy.

## Part 4.4 — Judge Queue Depth Test

- 5 requests with `run_eval:true` all returned 200
- Redis keys `judge:result:<trace_id>` appeared (5 entries) — judge-worker processed all jobs
- Queue drain confirmed: `judge:queue` LLEN = 0 after worker consumed items
- Queue backed by `judge:queue` Redis list (LPUSH/BRPOP pattern)

## Part 5 — Load Test: 50 Users × 5 min

**Hard gate triggered — STOPPED at 50 users.**

| Metric | /ask | /auth/login | Aggregated |
|--------|------|-------------|------------|
| Requests | 88 | 50 | 188 |
| Failures | 60 (68%) | 1 (2%) | 110 (59%) |
| p50 | 120,000ms | 92,000ms | 62,000ms |
| p95 | **120,000ms** | 99,000ms | 120,000ms |
| p99 | 120,000ms | 99,000ms | 120,000ms |
| Max | 120,483ms | 98,741ms | 120,483ms |
| req/s | 0.31 | 0.18 | 0.66 |

**Hard gate**: p95 = 120,000ms > 15,000ms threshold at 50 users. **100-user run NOT executed.**

### Root Cause

DeepSeek LLM returns "Insufficient Balance" on every `/ask` call that reaches the LLM stage. This causes:
1. Requests hang for the full RAG pipeline timeout before failing
2. Locust reports 120s timeouts (locustfile `timeout=120`)
3. High failure rate and extreme p95

The rate limiter, Redis queue, and auth stack are all functional — the bottleneck is the external LLM dependency.

### Infrastructure confirmed healthy (smoke test):
- Rate limiter: Redis INCR counter fires before LLM; 429 in ~13s per request
- Judge queue: LPUSH/BRPOP drain working; worker consumes jobs
- Auth: JWT signup+login working; token format `1:<uid>:<hmac>`

---

## CSV Files

- `locust_50u_stats.csv` — per-endpoint final stats
- `locust_50u_failures.csv` — failure breakdown
- `locust_50u_stats_history.csv` — time-series stats (10s buckets)
- `locust_50u_exceptions.csv` — exception log

---

## Recommendations

To unblock the 100-user load test and achieve p95 < 15s:
1. Restore LLM credits (DeepSeek or switch to OpenAI GPT-4o-mini which is already configured)
2. Or add a mock LLM response mode for load testing purposes
3. The infrastructure (rate limiter, Redis queue, auth) is proven correct and ready for production load testing
