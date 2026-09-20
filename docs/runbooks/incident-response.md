# Tessera — Incident Response Runbook

**Scope:** Tessera multi-tenant RAG API. All commands assume you have `kubectl` context set and `redis-cli` access to the cluster Redis.

---

## Severity Levels

| Sev | Condition | Response window |
|-----|-----------|-----------------|
| P0 | All tenants down / data leak suspected | Immediate |
| P1 | Single tenant degraded or eval gate broken | < 15 min |
| P2 | Elevated error rate, degraded latency | < 1 hr |
| P3 | Non-critical metric anomaly | Next business day |

---

## Runbook 1 — LLM Provider Circuit Open (P1)

**Symptoms:** `GET /health` returns `llm_circuit: OPEN`; `/ask` returns HTTP 503 with `Retry-After: 30`.

**Root cause:** 5 consecutive LLM failures tripped the circuit breaker.

**Steps:**
1. Check provider status page (DeepSeek / OpenAI).
2. Check API key quota: `curl -s https://api.deepseek.com/user/balance -H "Authorization: Bearer $DEEPSEEK_API_KEY"` — never print the key value in logs.
3. If provider is up, inspect API pod logs: `kubectl logs -l app=tessera-api --since=5m | grep "llm_error"`.
4. The breaker auto-recovers after 30 seconds (CIRCUIT_BREAKER_RECOVERY_SECONDS). Monitor `/health` for `llm_circuit: CLOSED`.
5. If provider is down: the fallback chain (`deepseek-flash` → `gpt-4o-mini`) should absorb traffic. Check `route` field in recent analytics: `tail -20 logs/analytics.jsonl | jq .route`.
6. If both providers are down: set `JUDGE_MODE=sync` and `CACHE_ENABLED=True` in the deployment env to maximise cache hit rate and skip background judging.

**Escalation:** if the circuit does not close within 10 minutes, page the on-call engineer.

---

## Runbook 2 — Judge Queue Backlog (P2)

**Symptoms:** `GET /metrics/latency` shows `published_judges > 50`; DLQ depth growing.

**Steps:**
1. Check queue depth: `redis-cli llen judge:queue`.
2. Check DLQ depth: `redis-cli llen judge:queue:dlq`.
3. Inspect DLQ entries: `redis-cli lrange judge:queue:dlq 0 4 | python -m json.tool`.
4. Check judge worker pod count: `kubectl get pods -l app=tessera-judge`.
5. Scale workers if needed: `kubectl scale deployment tessera-judge --replicas=5`.
6. If DLQ has malformed JSON: these are dead jobs — drain manually: `redis-cli del judge:queue:dlq`.
7. If DLQ has exhausted-retry jobs: inspect `_dlq_reason` field, fix root cause, then optionally re-queue: `redis-cli lrange judge:queue:dlq 0 -1 | jq -r '._raw' | xargs -I{} redis-cli lpush judge:queue {}`.

**Prevention:** `worker-hpa.yaml` scales judge workers on queue depth. Ensure the HPA `targetAverageValue` matches expected throughput.

---

## Runbook 3 — Cross-Tenant Data Suspected (P0)

**Symptoms:** Tenant reports answer containing another tenant's document text.

**Steps:**
1. **Immediately** set `CACHE_ENABLED=False` in deployment env and redeploy: `kubectl set env deployment/tessera-api CACHE_ENABLED=false`.
2. Flush the semantic cache: `redis-cli --scan --pattern 'sem:*' | xargs redis-cli del` — **verify** the pattern matches only Tessera cache keys first.
3. Collect evidence: check `logs/analytics.jsonl` for the affected `trace_id`, inspect `tenant` and `sources` fields.
4. Check cache key generation: `src/cache/semantic_cache.py::make_key` — tenant must be the first element of the raw key.
5. Check `use_tenant()` context: all retrieval paths must call `active_index_dir()` which reads from `tenant_context.active_tenant`.
6. Run the tenant isolation regression test: `pytest tests/test_semantic_cache.py::TestSemanticCacheTenantIsolation -v`.
7. Report to affected tenants and document the timeline before re-enabling cache.

---

## Runbook 4 — Authentication Failure Spike (P1)

**Symptoms:** 401 error rate spikes; `GET /health` still returns 200.

**Steps:**
1. Check if the `SESSION_SECRET` env var is set: `kubectl exec -it <api-pod> -- env | grep SESSION_SECRET` — **do not print the value**, only check it is non-empty.
2. Check token age: tokens older than `TESSERA_TOKEN_MAX_AGE_SECONDS` (default 86400 = 24h) are rejected. If the secret was recently rotated, all existing tokens are invalid.
3. After a secret rotation: instruct tenants to re-authenticate.
4. If the spike is external (brute force): check `auth.db` for rate limit enforcement; Redis-backed rate limiting (`RATE_LIMIT_PER_MINUTE`) should already be blocking offenders.

---

## Runbook 5 — Disk Full on Corpus/Index (P1)

**Symptoms:** Ingest endpoint returns 500; `df -h` shows < 1 GB free on the data volume.

**Steps:**
1. Check disk: `df -h /data`.
2. Identify large directories: `du -sh /data/corpus/* | sort -rh | head -10`.
3. Check Chroma index sizes: `du -sh /data/index/chroma/*`.
4. Delete stale tenant corpora (requires operator confirmation): `rm -rf /data/corpus/<tenant>` and the matching Chroma collection.
5. If on Kubernetes: resize the PVC or attach additional storage — refer to your cloud provider's PVC resize docs.

---

## Runbook 6 — Rate Limit Redis Unavailable (P2)

**Symptoms:** `GET /health` shows `redis: unreachable`; rate limiter logs `fail-open`.

**Behaviour:** The rate limiter degrades gracefully — all requests are allowed through (`fail_open=True`). No user-visible errors, but per-user limits are unenforced.

**Steps:**
1. Check Redis connectivity: `redis-cli -u $REDIS_URL ping`.
2. If Redis pod crashed: `kubectl describe pod -l app=redis` for OOM or eviction reason.
3. Restart Redis if needed: `kubectl rollout restart deployment/redis`.
4. Monitor: once Redis recovers, the rate limiter auto-reconnects on the next request.

---

## Runbook 7 — Disaster Recovery (Full Rebuild)

**When to use:** complete environment loss — cluster gone, data volume lost.

**Data that is recoverable:**
- Source documents: re-ingest from original corpus files.
- Auth database (`auth.db`): restore from last backup or recreate via `POST /register`.
- Analytics JSONL (`logs/analytics.jsonl`): restore from object storage backup if configured.
- Redis state: transient (queue, cache, rate-limit counters) — safe to lose.

**Steps:**
1. Provision new cluster (refer to `k8s/` manifests).
2. Restore `auth.db` from backup to a new PVC.
3. Restore corpus files to `/data/corpus/`.
4. Re-ingest all tenant corpora: `POST /ingest` per tenant.
5. Deploy API and judge worker: `kubectl apply -f k8s/`.
6. Smoke test: `pytest tests/ -x -q`.
7. Validate eval gate: `pytest ci/eval_gate.py -v`.

**RPO (Recovery Point Objective):** Limited to last backup frequency. Default: no automated backup — operator must configure one.  
**RTO (Recovery Time Objective):** Re-ingest time dominates. Estimate 10–30 minutes for a small corpus (< 1000 documents per tenant).

---

## Health Endpoint Reference

`GET /health` — returns JSON with component status:

```json
{
  "status": "ok",
  "llm_circuit": "CLOSED",
  "redis": "ok",
  "judge_queue_depth": 3,
  "dlq_depth": 0,
  "cache_hit_rate": 0.42
}
```

Any component showing a non-ok value should trigger the appropriate runbook above.
