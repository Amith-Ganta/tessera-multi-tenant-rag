# Runbook: DLQ Drain

**Severity:** P2  
**Estimated resolution time:** < 10 min  
**On-call trigger:** `GET /admin/dlq` shows growing `dlq_depth`; eval results absent for many requests

---

## 1. Detect

```bash
# Peek at the DLQ (admin credentials required)
TOKEN=<admin_token>
curl -sf http://127.0.0.1:8000/admin/dlq \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

Expected healthy response:
```json
{"dlq_depth": 0, "queue_depth": 0, "entries": []}
```

A growing `dlq_depth` indicates the judge worker is not consuming jobs successfully.

---

## 2. Diagnose the judge worker

```bash
# Docker
docker logs --tail 50 tessera-judge-worker

# Kubernetes
kubectl logs -n tessera deployment/tessera-judge-worker --tail=50
```

Common causes:
- OpenAI API key expired or rate-limited (429 errors in worker logs)
- Network partition between the worker and Redis or OpenAI
- OOM kill (check pod events: `kubectl describe pod -n tessera -l app=tessera-judge-worker`)
- DeepSeek API changes (check for model-not-found errors)

---

## 3. Fix the root cause

See `docs/runbooks/llm-provider-failover.md` for key rotation steps.

---

## 4. Restart the worker

```bash
# Docker
docker restart tessera-judge-worker

# Kubernetes
kubectl rollout restart deployment/tessera-judge-worker -n tessera
kubectl rollout status deployment/tessera-judge-worker -n tessera
```

---

## 5. Monitor queue drain

```bash
# Poll DLQ depth every 10 seconds
while true; do
  curl -sf http://127.0.0.1:8000/admin/dlq -H "Authorization: Bearer $TOKEN" \
    | python -c "import sys,json; d=json.load(sys.stdin); print(f'dlq={d[\"dlq_depth\"]} queue={d[\"queue_depth\"]}')"
  sleep 10
done
```

The DLQ should empty as the worker recovers. Queue depth may spike briefly as
re-queued jobs are processed.

---

## 6. Drain stale jobs (if worker is healthy but DLQ contains old failures)

If the DLQ contains jobs from a past outage that will never succeed
(for example, jobs referencing a document that has since been deleted):

```bash
curl -sf -X DELETE http://127.0.0.1:8000/admin/dlq \
  -H "Authorization: Bearer $TOKEN"
# Returns: {"drained": N}
```

This deletes all DLQ entries permanently. Eval results for those requests will not be
populated. The original `/ask` responses were already returned to users.

---

## 7. Notes

- DLQ entries have no TTL and persist indefinitely until drained or the worker retries them.
- DLQ drain requires admin-level credentials. Non-admin requests return HTTP 403.
- The DLQ does not have tenant isolation — all tenants' failed jobs are in one list.
