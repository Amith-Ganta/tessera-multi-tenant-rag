# Phase 8 OpenAI Load Test Summary

**Date**: 2026-09-20  
**Model**: `gpt-4o-mini` (switched from DeepSeek after zero-balance failure in prior run)  
**Rate limit**: `RATE_LIMIT_PER_MINUTE=100` (verified in container env)  
**Smoke test**: HTTP 200, `"model": "gpt-4o-mini"` confirmed before tests began

---

## 50-User Run (`run_50_openai`)

| Metric | /ask |
|--------|------|
| Requests | 66 |
| Failures | 52 (78.8%) |
| p50 | 120,000 ms |
| p95 | 121,000 ms |
| p99 | 121,000 ms |
| Max | 120,730 ms |
| RPS | 0.30 |
| Errors/s | 0.24 |

**Failure breakdown**:
- 50× `ReadTimeout` (Locust 120 s client timeout — requests hung in queue)
- 2× `RemoteDisconnected` (server closed connection mid-flight)

**Auth overhead** (not the primary concern, but explains run dynamics):
- `/auth/signup` — 49/50 users got 400 (duplicate account — expected; `loadtest@tessera.local` already exists)
- `/auth/login` — all 50 succeeded, avg 55.9 s (bcrypt under concurrent load)

---

## 100-User Run (`run_100_openai`)

| Metric | /ask |
|--------|------|
| Requests | 50 |
| Failures | 46 (92%) |
| p50 | 120,000 ms |
| p95 | 120,000 ms |
| p99 | 120,000 ms |
| Max | 120,367 ms |
| RPS | 0.24 |
| Errors/s | 0.22 |

**Note**: Fewer /ask requests than the 50-user run because more of the 5-minute window was consumed by the auth bottleneck at 100 concurrency.

---

## Hard-Gate Result

Both runs hit the **hard gate** (`p95 > 15 s`):

| Run | /ask p95 | Gate threshold | Result |
|-----|----------|---------------|--------|
| 50 users | 121,000 ms | 15,000 ms | **FAIL** |
| 100 users | 120,000 ms | 15,000 ms | **FAIL** |

---

## Root Cause Analysis

The bottleneck is **not the LLM provider** (OpenAI calls themselves completed; 14 /ask requests succeeded in the 50-user run with avg ~15–25 s). The bottleneck is **server-side concurrency architecture**:

1. **Single uvicorn worker (no `--workers` flag)** — the API runs a single async event loop. Concurrent /ask requests queue behind each other rather than executing in parallel.

2. **bcrypt is CPU-blocking** — each `/auth/login` call runs bcrypt verification synchronously on the event loop, blocking all other requests for ~1–2 s per verification. With 50–100 users logging in simultaneously, this creates a 60–170 s auth bottleneck that eats the test window before /ask starts.

3. **LLM call latency amplified by serialisation** — even a 3–5 s OpenAI call becomes a 120 s timeout when 50 requests queue behind a blocked event loop.

**Fix path** (out of scope for this test run, captured here for record):
- Run uvicorn with `--workers $(nproc)` (multi-process, each worker gets one CPU)
- Offload bcrypt to a thread pool (`asyncio.run_in_executor`)
- Or: use `argon2-cffi` with its async-friendly interface

---

## Comparison to Prior DeepSeek Run

| Metric | DeepSeek (Phase 8 run 1) | OpenAI (Phase 8 run 2) |
|--------|--------------------------|------------------------|
| Model | deepseek/deepseek-chat | gpt-4o-mini |
| Failure cause | Insufficient balance (100%) | Concurrency/bcrypt bottleneck |
| /ask p95 @ 50 users | 120,000 ms | 121,000 ms |
| Successful /ask calls | 0 | 14 of 66 |

OpenAI calls reach the model and return answers; DeepSeek returned errors immediately. The concurrency bottleneck existed in both runs but was masked in the DeepSeek run by immediate API errors.
