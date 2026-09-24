# Tessera — Critical Review

**Date:** 2026-09-24  
**Reviewer:** Automated senior engineering review  
**Scope:** All Phase A and Phase B changes; full system state after 264 tests pass.  
**Method:** Code inspection of every changed file. No fabricated test results.

---

## C1 — Security: Auth and Secrets

### HMAC Token Pipeline

`src/auth/auth.py` + `src/api/app.py`

Token construction: `{user_id}:{issued_ts}:{HMAC-SHA256 of user_id:issued_ts}`.  
Token verification: signature recomputed with `hmac.compare_digest` (constant-time), then expiry checked.  
Prod guard: `TESSERA_ENV=prod` + missing/default secret raises `RuntimeError` at startup.

**Verdict: PASS.** No gaps in the auth pipeline.

### API Key Exposure

Grep check: no `LANGFUSE_*`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `SECRET_KEY` literals appear in any source file or test fixture. All secrets read from env vars.

**Verdict: PASS.**

### SSRF Guard (Phase A1)

`src/security/ssrf.py` — `SSRFGuard.check()` resolves the hostname via `socket.getaddrinfo`, then checks each resolved IP against RFC 1918 ranges, loopback, link-local, and known metadata endpoints. 11 tests cover private ranges, public ranges, metadata IPs, and the `localhost` intercept issue (always uses `127.0.0.1`).

**Verdict: PASS.**

---

## C2 — Tenant Isolation

### Vector Store

`src/rag/tenant_context.py` — `use_tenant()` context manager sets `_active_tenant` in `contextvars.ContextVar`. `active_index_dir()` reads from the same `ContextVar`. Every retrieval call goes through `active_index_dir()`. No cross-request bleed is possible via a shared module-level variable.

**Verdict: PASS.**

### Semantic Cache

`src/cache/semantic_cache.py` — `make_key()` prepends tenant as the first element of the SHA-256 digest input. Phase 2 regression tests (`TestSemanticCacheTenantIsolation`) verified this in 4 cases.

**Verdict: PASS.**

### Judge Results

`src/judge/redis_queue.py` — keys are `judge:result:<tenant>:<trace_id>`. `get_result()` requires tenant as a parameter. `invalidate_by_tenant()` uses `SCAN` with tenant prefix filter.

**Verdict: PASS.**

### Rate Limiter

`src/resilience/rate_limiter.py` — key is `rl:<user_id>`. Fail-open on Redis loss (ADR-015).

**Verdict: PASS — by design (documented).**

### Tenant Governor

`src/resilience/tenant_governance.py` — three Redis counters per tenant, all fail-closed. ADR-016 documents the rationale.

**Verdict: PASS.**

---

## C3 — Cost Observability Module

`src/observability/cost.py`

- `RATES` dict covers all models used (`deepseek-flash`, `gpt-4o-mini`, `gpt-4o`, `text-embedding-3-small`, `gpt-4o-mini-judge`).
- Unknown model returns `0.0` (no exception) — correct fail-safe behaviour.
- `over_cap()` returns `False` when `TESSERA_DAILY_SPEND_USD_CAP` is absent — correct (cap disabled).
- `reset()` is available for test teardown; the accumulator is in-process and resets on pod restart.
- `spend_so_far()` / `over_cap()` are not atomic under concurrent load — documented in ADR-018 §Consequences and D6 in THREAT_MODEL.md.

**Open risk acknowledged: D6 (non-atomic cap under parallel load). The mitigation path (Redis INCR) is documented in ADR-018.**

**Verdict: PASS with documented limitation.**

---

## C4 — DLQ Admin Endpoints

`src/api/app.py` + `src/judge/redis_queue.py`

- `GET /admin/dlq` requires `auth.is_admin(email)` — returns 403 for non-admin.
- `DELETE /admin/dlq` same admin check; calls `drain_dlq()` which returns count of deleted entries.
- `peek_dlq()` returns at most 10 entries by default (no unbounded LRANGE).
- DLQ entries have no TTL and persist until drained — documented as open risk D5 in THREAT_MODEL.md. `JUDGE_DLQ_MAX_DEPTH` env var is not yet implemented.
- I7 in THREAT_MODEL.md: DLQ entries contain question text visible to all admins.

**Verdict: PASS with two documented open risks (D5, I7).**

---

## C5 — Shadow Eval Promotion Gate

`src/rag/promotion_gate.py`

- Fail-closed: `compare()` returns `REJECT` if `n_samples < MIN_SHADOW_SAMPLES` (5).
- Fail-closed: `compare()` returns `REJECT` if the candidate does not exceed `PROMOTION_MARGIN` (0.02) above the baseline mean score.
- `run_shadow_experiment()` constructs the shadow index and queries both baseline and shadow with the same questions; results are aggregated by `compare()`.
- No external write path to the shadow index — tamper risk T6 in THREAT_MODEL.md is low.

**Verdict: PASS. Gate is correctly fail-closed.**

---

## C6 — RAG Quality Signals

`src/observability/rag_signals.py`

- `record_rag_signals()` is wrapped in a broad `except Exception` — any failure returns `{}` without affecting the `/ask` path.
- Record written as `{"type": "rag_quality", ...}` to `logs/analytics.jsonl` via `log_analytics()`.
- No `trace_id` linking to the originating `ask` record — documented in ADR-020 §Consequences as a join-on-tenant+timestamp limitation.

**Verdict: PASS. The fail-safe and the join limitation are both documented.**

---

## C7 — Kubernetes Manifests

`k8s/deployment.yaml`, `k8s/service.yaml`

- `tessera-api`: `replicas: 2`, `maxUnavailable: 0` ensures zero-downtime rolling update.
- Liveness probe on `/health` (10s initial delay, 5s period) — compatible with FastAPI startup time.
- Readiness probe on `/health` (5s initial delay, 3s period) — tight; if startup takes >5s, the first probe fires before the app is ready. Acceptable for a dev/staging manifest.
- Env from `tessera-secrets` (sensitive) + `tessera-config` (non-sensitive) — correctly separated.
- PVC `data-volume` mounted at `/app/data` — Chroma and corpus will persist across pod restarts.
- `logs-volume` is `emptyDir` — analytics.jsonl is lost on pod restart. A production deployment should mount this on a PVC or forward to an external log sink.
- `tessera-judge-worker`: `terminationGracePeriodSeconds: 120` — allows in-flight judge jobs to complete.

**Open item: logs-volume is emptyDir — analytics.jsonl not durable across restarts.**

**Verdict: PASS for dev/staging. Logs durability is a known production gap.**

---

## C8 — Test Suite Integrity

### Gate evidence

Phase B test gate was verified at 264 passing tests (`uv run python -m pytest tests/ -q`).

### Coverage distribution

| Test file | Tests | What it covers |
|-----------|-------|---------------|
| test_cost_observability.py | 14 | B1 cost module |
| test_queue_dlq.py | 12 | B2/B3 queue metrics + DLQ |
| test_rag_signals.py | 22 | B4 RAG quality signals |
| test_shadow_eval.py | 15 | B5 promotion gate |
| test_ssrf.py | 11 | A1 SSRF guard |
| test_cors.py | 5 | A2 CORS |
| test_concurrency.py | 4 | A3 SSE bulkhead guard |
| test_a2a_analytics.py | 2 | A4 A2A analytics |
| test_embedding_resilience.py | 6 | MM-01 embedding fallback |
| test_queue_publish_honesty.py | 6 | MM-02 eval status honesty |
| test_tenant_governance.py | 8 | 3B tenant governor |
| test_document_delete.py | 7 | GAP-01/02/03 deletion |
| test_tenant_delete.py | 5 | GAP-04/07/09 |
| test_token_expiry.py | 7 | SEC-01 token expiry |
| (+ 16 further test files) | 140 | Phases 1–3 |

### Gaps not covered by tests

- `loadtests/locustfile.py` — no integration test that actually runs Locust; only unit-level task mix is verified by inspection.
- `k8s/deployment.yaml` — no manifest validation test (e.g., `kubectl dry-run`); schema is valid by inspection.
- `docs/runbooks/` — runbooks are prose; no automated verification of the shell commands they contain.
- Multi-replica cost cap atomicity — `over_cap()` race condition (D6) has no test because it requires parallel processes.

**Verdict: PASS. Gaps are documented and non-blocking for the current single-operator deployment.**

---

## Summary

| Section | Verdict | Open items |
|---------|---------|------------|
| C1 — Auth & Secrets | PASS | — |
| C2 — Tenant Isolation | PASS | — |
| C3 — Cost Observability | PASS (documented limitation) | D6: non-atomic cap |
| C4 — DLQ Admin | PASS (documented limitations) | D5: no cap; I7: question text in DLQ |
| C5 — Shadow Eval Gate | PASS | — |
| C6 — RAG Quality Signals | PASS (documented limitation) | No trace_id join key |
| C7 — Kubernetes Manifests | PASS for dev/staging | logs emptyDir not durable |
| C8 — Test Suite Integrity | PASS | Locust/k8s/runbook gaps |

No blocking issues found. All open items are documented in ADRs, THREAT_MODEL.md, or inline in the relevant runbook/manifest.
