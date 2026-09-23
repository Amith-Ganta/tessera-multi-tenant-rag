# Disaster Recovery

**Tessera Multi-Tenant RAG API**  
Last updated: 2026-09-24

---

## 1. Recovery Objectives

| Tier | Scenario | RTO Target | RPO Target |
|---|---|---|---|
| P0 | Full process crash (OOM, segfault) | < 2 min | 0 (stateless request path) |
| P0 | Redis outage (cache, queue, rate limiter) | < 5 min | 0 (Redis is non-authoritative for documents) |
| P1 | Corrupt or lost Chroma index | < 30 min per tenant | Full corpus retained (raw docs on disk) |
| P1 | Corrupt or lost user database | < 15 min | Last filesystem backup (see §4) |
| P2 | Analytics log corruption | N/A (non-functional) | Last filesystem backup |
| P2 | DLQ flood (judge worker down) | < 10 min | 0 (eval is advisory; answers are already returned) |

---

## 2. Failure Scenarios and Recovery Steps

### 2.1 API Process Crash

**Symptom:** All requests return connection refused; health check fails.

**Impact:** All in-flight requests are lost. The semantic cache (in-process) is lost.
The judge result ring buffer (in-process) is lost. All durable data (disk, Redis, SQLite)
is intact.

**Recovery:**
1. Restart the process (`docker restart tessera-api` or `kubectl rollout restart`).
2. The process starts cold; the vectorstore cache is rebuilt on first request per tenant.
3. Verify: `GET /health` returns `{"status": "ok"}`.
4. Verify: `GET /metrics` shows non-zero request counters after a test `/ask`.

No data is lost. Semantic cache misses will be elevated for a few minutes while the
cache warms.

---

### 2.2 Redis Outage

**Symptom:** Redis commands fail; logs show `Redis client init failed` or connection errors.

**Impact by subsystem:**

| Subsystem | Behaviour Without Redis | Authoritative Store Intact? |
|---|---|---|
| Rate limiter | Fail-open (no rate limiting) | N/A |
| Tenant governor | Fail-closed (all requests blocked for governed tenants) | N/A |
| Judge queue | Jobs not submitted; eval results show "pending" indefinitely | Yes — raw docs + Chroma untouched |
| Semantic cache | Every request is a cache miss; LLM called on every query | N/A |
| Checkpointer (Redis) | Redis path skipped; SQLite checkpointer used as fallback | Yes — SQLite on disk |
| DLQ | peek_dlq / drain_dlq return 0 / [] | N/A |

**Recovery:**
1. Restore Redis (`docker restart tessera-redis` or re-provision the managed instance).
2. Verify Redis is reachable: `redis-cli -u $REDIS_URL ping` → `PONG`.
3. Restart the API process to reinitialise the Redis client (lazy init retries on next call,
   but an explicit restart is faster).
4. Outstanding judge jobs that failed to enqueue during the outage are lost at-most-once.
   Re-submit affected queries if eval results are required.

---

### 2.3 Corrupt or Missing Chroma Index

**Symptom:** `/ask` returns empty contexts or a `ChromaDB` exception in logs.
`GET /health` may still return `ok` (Chroma is checked lazily).

**Impact:** Retrieval fails for the affected tenant. Other tenants are unaffected.

**Recovery (per-tenant index rebuild):**
1. Identify the tenant slug from logs or from `GET /admin/analytics`.
2. Confirm raw documents are present: `ls data/tenants/<slug>/corpus/`.
3. If raw documents exist, trigger a re-index:
   - `POST /upload` with any document already in the corpus (the upload endpoint wipes and
     rebuilds the full index from all files in the corpus directory).
   - Or call `src.rag.ingest.build_tenant_index(slug)` directly from a management script.
4. Verify: send a test `/ask` for that tenant and confirm non-empty `sources`.

**If raw documents are also missing**, restore from the last filesystem backup (see §4),
then follow the re-index steps above.

---

### 2.4 Corrupt or Missing User Database (`tessera_users.db`)

**Symptom:** All authentication attempts fail; `/health` check may pass but all
  `/ask` and other authenticated endpoints return 401.

**Impact:** All tenants locked out. Data on disk (documents, Chroma) is intact.

**Recovery:**
1. Stop the API process.
2. Restore `tessera_users.db` from the last backup.
3. Restart the API process.
4. Verify: authenticate as a known user and confirm `GET /health` passes.

**If no backup is available:** The database schema is created on startup if absent
(`src/auth/auth.py` calls `init_db()` on first import). Re-create user accounts via
the `POST /register` endpoint. Tenant slugs are derived from `user-<user_id>` so the
same slug is recovered when the same usernames are re-registered in the same order.
The Chroma indices remain on disk keyed by slug — they are immediately accessible after
re-registration.

---

### 2.5 DLQ Flood (Judge Worker Down)

**Symptom:** `GET /admin/dlq` shows a rapidly growing `dlq_depth`; eval results are
  all `"pending"` or absent.

**Impact:** `/ask` answers are returned normally (eval is async and advisory).
  Quality scores are unavailable until the worker recovers.

**Recovery:**
1. Diagnose the worker: `docker logs tessera-judge-worker` or check k8s pod logs.
2. Fix the root cause (most common: OpenAI key expired, network partition, OOM).
3. Restart the judge worker: `docker restart tessera-judge-worker`.
4. If DLQ contains stale jobs from the outage that are no longer relevant, drain it:
   `DELETE /admin/dlq` (admin only).
5. Verify: `GET /admin/dlq` returns `dlq_depth: 0` (or a decreasing count).

---

### 2.6 Full Disk — Data Volume

**Symptom:** Uploads fail with 500; analytics log writes silently fail; Chroma writes fail.

**Impact:** Ingest is blocked. Existing queries may still succeed from cached or existing indices.

**Recovery:**
1. Identify the large consumers: analytics log (`logs/analytics.jsonl`) is unbounded and
   commonly the first to grow.
2. Rotate / truncate the analytics log:
   - `mv logs/analytics.jsonl logs/analytics.jsonl.bak && touch logs/analytics.jsonl`
3. If corpus files are large, remove unused tenant documents via `DELETE /documents/{filename}`.
4. Alert to add disk capacity or mount a larger volume.

---

## 3. Degradation Summary

The following table shows the fail-safe behaviour of each subsystem when its upstream
dependency is unavailable, derived from `docs/DEPENDENCY_FAILURE_MATRIX.md`.

| Subsystem | Upstream Dependency | Fail Behaviour | User Visible? |
|---|---|---|---|
| LLM generation | LiteLLM / provider | Circuit breaker opens after 5 failures → 503 | Yes |
| Retrieval | Chroma (disk) | Exception → 500 | Yes |
| Rate limiter | Redis | Fail-open (no limiting) | No |
| Tenant governor | Redis | Fail-closed (block) | Yes |
| Semantic cache | In-process memory | Cache miss; LLM called | No (latency increase only) |
| Judge queue | Redis | Job not submitted; eval skipped | No (eval advisory) |
| Checkpointer | Redis | Falls back to SQLite | No |
| DLQ drain | Redis | Returns 0 / [] | No |

---

## 4. Backup Schedule (Recommended)

| Item | Path | Frequency | Method |
|---|---|---|---|
| Raw documents | `data/tenants/*/corpus/` | Daily | `tar -czf corpus-$(date +%F).tar.gz data/tenants/` |
| Chroma indices | `data/index/tenants/*/chroma/` | Daily (or after each upload) | `tar -czf index-$(date +%F).tar.gz data/index/` |
| User database | `tessera_users.db` | Daily | `cp tessera_users.db backups/tessera_users-$(date +%F).db` |
| Checkpoint DB | `data/checkpoints.sqlite3` | Daily (off-peak) | `cp data/checkpoints.sqlite3 backups/checkpoints-$(date +%F).sqlite3` |
| Analytics log | `logs/analytics.jsonl` | Weekly rotation | `logrotate` or manual `mv` + `touch` |

Chroma indices **can be regenerated** from raw documents at any time by re-uploading them.
Backing up the raw corpus is therefore sufficient for full recovery of retrieval capability,
though backing up the index avoids a potentially expensive re-embedding run.

---

## 5. Runbook References

| Scenario | Runbook |
|---|---|
| API down / degraded | `docs/runbooks/incident-response.md` |
| Redis outage | `docs/runbooks/redis-failover.md` (planned) |
| Provider rate limit / 5xx | `docs/runbooks/llm-provider-failover.md` (planned) |
| Chroma index corruption | `docs/runbooks/index-rebuild.md` (planned) |
| DLQ management | `docs/runbooks/dlq-drain.md` (planned) |
| Cost cap breach | `docs/runbooks/cost-cap.md` (planned) |
