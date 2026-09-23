# Runbook: Redis Failover

**Severity:** P0  
**Estimated resolution time:** < 5 min (restart) / < 30 min (re-provision)  
**On-call trigger:** Redis connection errors in logs; `GET /health` reports degraded subsystems

---

## 1. Detect

```bash
# Confirm Redis is unreachable from the host
redis-cli -u $REDIS_URL ping
# Expected: PONG
# Failure: Could not connect to Redis / Connection refused
```

Check API logs for patterns:
- `Redis client init failed`
- `judge queue publish error`
- `rate limiter redis error`

---

## 2. Impact Assessment

| Subsystem | Behaviour | Visible to users? |
|---|---|---|
| Rate limiter | **Fail-open** — all limits bypassed | No (users get more requests through) |
| Tenant governor | **Fail-closed** — governed tenants blocked | Yes — 429/503 |
| Judge queue | Jobs dropped; eval stuck "pending" | No (eval is advisory) |
| Semantic cache | All cache misses; LLM called every request | No (latency increase only) |
| Redis checkpointer | Falls back to SQLite; workflows continue | No |
| DLQ admin endpoints | Return 0 / [] | No |

---

## 3. Restart (Docker)

```bash
docker restart tessera-redis
sleep 5
redis-cli -u $REDIS_URL ping
```

---

## 4. Restart (Kubernetes)

```bash
kubectl rollout restart deployment/redis -n tessera
kubectl rollout status deployment/redis -n tessera
```

After Redis recovers, restart the API process to reinitialise the client:

```bash
kubectl rollout restart deployment/tessera-api -n tessera
kubectl rollout status deployment/tessera-api -n tessera
```

---

## 5. Verify

```bash
curl -sf http://127.0.0.1:8000/health | python -m json.tool
# Expect: {"status": "ok"}
redis-cli -u $REDIS_URL info keyspace
# Should show judge: and checkpoint: keys once traffic resumes
```

---

## 6. Post-incident

- Outstanding judge jobs that failed to enqueue during the outage are lost (at-most-once delivery). If eval results are needed for affected queries, re-submit them.
- Check `GET /admin/dlq` (admin) for any stale DLQ entries from the outage. Drain if necessary: `DELETE /admin/dlq`.
- File a post-incident note if the outage lasted > 5 minutes.
