# Tessera — Threat Model

**Methodology:** STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege).  
**Scope:** API gateway, RAG pipeline, judge queue, semantic cache, auth module, A2A agent layer.  
**Date:** 2026-09-20. **Author:** Security audit, Phase 3.

---

## Trust Boundary Map

```
[External caller]
    │  HTTPS
    ▼
[FastAPI /ask, /eval, /budget, /ingest]
    │  HMAC token validated
    ▼
[Auth module — auth.db (SQLite)]
    │
    ├──▶ [Rate limiter — Redis]
    │
    ├──▶ [Bulkhead / circuit breaker — in-process]
    │
    ├──▶ [RAG pipeline — tenant-scoped Chroma]
    │         │
    │         └──▶ [LiteLLM → DeepSeek / OpenAI]
    │
    ├──▶ [Semantic cache — in-process dict]
    │
    └──▶ [Judge queue — Redis LPUSH]
              │
              ▼
         [Judge worker — separate process]
              │
              └──▶ [DeepEval + LLM judge]
```

---

## STRIDE Analysis

### S — Spoofing

| ID | Threat | Asset | Mitigation | Residual risk |
|----|--------|-------|------------|---------------|
| S1 | Forged HMAC token | /ask, /eval | HMAC-SHA256 with `SESSION_SECRET`; `hmac.compare_digest` prevents timing attacks | Low — secret rotation invalidates all tokens |
| S2 | Token replay after expiry | All authenticated endpoints | `_verify_token` checks `time.time() - issued_at > TOKEN_MAX_AGE` | Low — 24 h window; reduce for high-security tenants |
| S3 | Tenant ID spoofing in request body | RAG pipeline | Tenant extracted from auth token, not from request body; `TENANT_ID_PATTERN = r'^[a-z0-9_-]{1,64}$'` validated at registration | Low |

### T — Tampering

| ID | Threat | Asset | Mitigation | Residual risk |
|----|--------|-------|------------|---------------|
| T1 | Corpus file injection via ingest | Chroma index | Ingest requires auth; files stored per-tenant in `/data/corpus/<tenant>/` | Medium — no virus scan or content type whitelist on uploaded files |
| T2 | Redis job tampering (queue) | Judge queue | Redis not exposed externally; judge worker deserialises with json.loads (safe) | Low — internal network only |
| T3 | Semantic cache poisoning | Cache | Cache key is SHA-256 of `tenant+query+chunks+model`; cache is in-process; no external write path | Low |
| T4 | Prompt injection via user question | LLM generation | Blocked-phrase list in `/ask` (`BLOCKED_PHRASES`); question length limit | Medium — phrase list is not exhaustive; LLM-based input guard would improve coverage |

### R — Repudiation

| ID | Threat | Asset | Mitigation | Residual risk |
|----|--------|-------|------------|---------------|
| R1 | User denies making a query | Billing, audit | `auth.log_query` writes user_id, question, answer, route, tokens, cost to auth.db | Low — database is authoritative; not tamper-evident |
| R2 | Analytics log tampering | `logs/analytics.jsonl` | JSONL append-only by design; no integrity hash | Medium — log file is writable by the API process; consider external log forwarding |

### I — Information Disclosure

| ID | Threat | Asset | Mitigation | Residual risk |
|----|--------|-------|------------|---------------|
| I1 | Cross-tenant cache leak | Semantic cache | `make_key()` includes tenant as first element; ADR-008 tenant isolation regression tests | Low — fix verified in Phase 2 |
| I2 | Cross-tenant Chroma reads | Chroma vector store | `use_tenant()` context manager sets per-request index dir; `active_index_dir()` reads from `contextvars` | Low |
| I3 | Secrets in logs | API keys, SESSION_SECRET | `NEVER_LOG` constraint enforced in code review; no f-string interpolation of key values in any source file | Low — human error remains possible; consider secret scanning in CI |
| I4 | User data in error responses | /ask 500 errors | FastAPI exception handlers return generic messages; stack traces not forwarded to caller | Low |
| I5 | Analytics JSONL world-readable | User questions, answers | File created with default OS permissions; recommend `chmod 600` in deployment | Medium |

### D — Denial of Service

| ID | Threat | Asset | Mitigation | Residual risk |
|----|--------|-------|------------|---------------|
| D1 | Request flood per user | /ask | Redis fixed-window rate limiter: `RATE_LIMIT_PER_MINUTE=100` per user_id | Low — fails open on Redis loss (by design) |
| D2 | LLM provider saturation | LiteLLM | Circuit breaker opens after 5 failures; bulkhead caps concurrency (`MAIN_POOL_SIZE=10`) | Low |
| D3 | Judge queue exhaustion | Redis memory | `JUDGE_QUEUE_MAX_DEPTH=100` cap; `/ask` returns 429 when exceeded | Low |
| D4 | Large corpus ingest blocking embeddings | Embedding API | No ingest rate limit currently; operator must cap per-tenant corpus size manually | Medium — add ingest queue for large uploads |
| D5 | DLQ unbounded growth | Redis memory | DLQ has no cap; if all retried jobs fail, DLQ fills; manual drain required | Medium — add DLQ max depth + alert |

### E — Elevation of Privilege

| ID | Threat | Asset | Mitigation | Residual risk |
|----|--------|-------|------------|---------------|
| E1 | Non-admin accessing /budget of another user | Budget endpoint | `get_current_user` extracts user_id from verified token; `/budget` only reads that user's own spend | Low |
| E2 | Non-admin accessing /admin/analytics | Analytics endpoint | `auth.is_admin()` check on email; 403 for non-admins | Low |
| E3 | SQL injection via user input | auth.db | All auth queries use parameterised statements (SQLite) | Low |
| E4 | Path traversal via tenant_id or filename | Corpus directories | Tenant ID regex `^[a-z0-9_-]{1,64}$` blocks traversal characters; filenames sanitised in ingest | Low |

---

## Open Risks (Medium and Above)

| ID | Risk | Recommended action |
|----|------|--------------------|
| T1 | No content type validation on ingest | Add allowed MIME type whitelist (PDF, TXT, MD only) |
| T4 | Phrase-list prompt injection filter is incomplete | Add LLM-based input classifier or use a guardrails library |
| R2 | Analytics log not tamper-evident | Forward logs to external sink (S3, CloudWatch) with integrity checks |
| I5 | Analytics JSONL world-readable | Set `chmod 600` in Dockerfile / entrypoint |
| D4 | No ingest rate limit | Add per-tenant ingest queue or size cap |
| D5 | DLQ has no cap | Add `JUDGE_DLQ_MAX_DEPTH` and alert when threshold exceeded |

---

## Out of Scope

- Network-layer controls (TLS termination, WAF) — operator responsibility.
- Key management (HSM, Vault) — out of scope for this single-operator deployment.
- Supply chain attacks on Python dependencies — use `pip-audit` in CI.
