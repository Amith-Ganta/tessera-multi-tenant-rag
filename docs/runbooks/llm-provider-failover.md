# Runbook: LLM Provider Failover

**Severity:** P0  
**Estimated resolution time:** < 2 min (circuit breaker auto-recovers) / < 30 min (key rotation)  
**On-call trigger:** HTTP 503 `CircuitOpenError` on `/ask`; LiteLLM 429 or 5xx in logs

---

## 1. Detect

```bash
# Check for circuit-breaker opens in logs
grep -i "circuit.*open\|CircuitOpenError\|litellm.*429\|litellm.*5xx" logs/app.log

# Check /metrics for error rate spike
curl -sf http://127.0.0.1:8000/metrics | grep ask_errors
```

Common causes:
- DeepSeek API key expired or rate-limited
- OpenAI key expired or quota exceeded
- Provider outage (check provider status page)

---

## 2. Impact

| Condition | Behaviour |
|---|---|
| Primary model fails ≤ 5 times (CLOSED breaker) | LiteLLM retries ×2, then falls back to `gpt-4o-mini` |
| Circuit OPEN (≥ 5 consecutive failures) | All LLM calls return HTTP 503 `Retry-After: 30` for 30 s |
| Circuit HALF_OPEN (30 s elapsed) | One probe call; if successful, circuit closes |
| All fallbacks exhausted | HTTP 500 |

---

## 3. Immediate mitigation — wait for auto-recovery

The circuit breaker enters HALF_OPEN after 30 seconds and tests the provider. If the
provider has recovered, the breaker closes automatically. No action needed unless the
outage persists > 5 minutes.

---

## 4. Rotate API keys

```bash
# Update the Kubernetes secret
kubectl create secret generic tessera-secrets \
  --from-literal=DEEPSEEK_API_KEY=<new_key> \
  --from-literal=OPENAI_API_KEY=<new_key> \
  --dry-run=client -o yaml | kubectl apply -f -

# Rolling restart to pick up new keys
kubectl rollout restart deployment/tessera-api -n tessera
kubectl rollout status deployment/tessera-api -n tessera
```

---

## 5. Verify

```bash
# Send a test /ask after restart
curl -sf -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"test"}' | python -m json.tool
# Expect: 200 with answer field populated
```

---

## 6. Rate limit mitigation

If `DeepEval calls/min > 500` (judge pool hitting OpenAI 429s):
1. Reduce `JUDGE_POOL_SIZE` (default 3) to throttle concurrent judge jobs.
2. Or set `JUDGE_MODE=disabled` temporarily to stop eval until load drops.
3. The async judge path (default) never blocks `/ask`, so users are unaffected.
