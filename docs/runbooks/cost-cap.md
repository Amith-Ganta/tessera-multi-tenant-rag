# Runbook: Cost Cap Breach

**Severity:** P1  
**Estimated resolution time:** < 5 min to stop; < 1 h to diagnose  
**On-call trigger:** Unexpected cloud spend spike; users receiving 500 errors with "daily spend cap exceeded"

---

## 1. Detect

Check whether the daily cap is currently exceeded:

```bash
# Query the metrics endpoint for spend
curl -sf http://127.0.0.1:8000/metrics | grep tessera_cost

# Or check the analytics log for recent high-cost requests
grep '"type":"rag_quality"' logs/analytics.jsonl | python -c "
import sys, json
records = [json.loads(l) for l in sys.stdin if l.strip()]
costs = [r.get('estimated_cost_usd',0) for r in records if r.get('estimated_cost_usd')]
print(f'Total: \${sum(costs):.4f}  Max single: \${max(costs, default=0):.6f}  Count: {len(costs)}')
"
```

---

## 2. Impact

When `over_cap()` returns True (spend > `TESSERA_DAILY_SPEND_USD_CAP` > 0):
- LLM calls should be rejected before submission.
- Current implementation: `over_cap()` is available in `src/observability/cost.py`
  but cap enforcement must be wired into the `/ask` path explicitly.
- If not yet wired: the cap is advisory only. No automatic rejection occurs.

---

## 3. Immediate mitigation — lower or enforce the cap

```bash
# Kubernetes: update the ConfigMap
kubectl edit configmap tessera-config -n tessera
# Set TESSERA_DAILY_SPEND_USD_CAP to the desired value (e.g., "5.00")

# Restart to pick up the new value (env is read at startup)
kubectl rollout restart deployment/tessera-api -n tessera
```

---

## 4. Reset the in-process accumulator

The accumulator resets to 0 on process restart. If the cap was reached because
of a test run or misconfiguration (not genuine overspend), a restart is sufficient:

```bash
# Kubernetes
kubectl rollout restart deployment/tessera-api -n tessera

# Docker
docker restart tessera-api
```

---

## 5. Diagnose the high-spend source

```bash
# Find tenants with most spend today
grep '"type":"rag_quality"' logs/analytics.jsonl | python -c "
import sys, json
from collections import defaultdict
totals = defaultdict(float)
for line in sys.stdin:
    try:
        r = json.loads(line)
        if r.get('type') == 'rag_quality':
            totals[r.get('tenant','?')] += r.get('estimated_cost_usd', 0)
    except json.JSONDecodeError:
        pass
for t, c in sorted(totals.items(), key=lambda x: -x[1]):
    print(f'{t}: \${c:.4f}')
"
```

If one tenant accounts for a disproportionate share, review whether they are calling
`/ask` in a loop or uploading very large documents that trigger expensive embeddings.

---

## 6. Notes

- The accumulator is in-process only. Multi-replica deployments share no state.
  Each pod has its own counter. Total spend is the sum across all pods.
- See `docs/adr/ADR-018.md` for the decision to use in-process accounting and
  the deferred multi-replica coordination enhancement.
- The daily cap is per-process, not per-tenant. Per-tenant budgets are not implemented.
