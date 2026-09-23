# Runbook: Rolling Restart

**Severity:** Maintenance  
**Estimated resolution time:** < 5 min  
**Use when:** Deploying a new image, rotating secrets, picking up env-var config changes

---

## 1. Pre-restart checklist

- [ ] New image is tagged and pushed to the container registry.
- [ ] Secrets / ConfigMap changes have been applied (`kubectl apply`).
- [ ] `minReplicas: 2` in `k8s/hpa.yaml` ensures zero-downtime rollout.
- [ ] No active A2A workflows expected to be mid-flight (they will resume from SQLite checkpoint on next request; Redis checkpoints will be lost — see `docs/DISASTER_RECOVERY.md §2.2`).

---

## 2. Update the deployment image

```bash
# Tag the new build
docker build -t tessera-api:<version> .
docker tag tessera-api:<version> <registry>/tessera-api:<version>
docker push <registry>/tessera-api:<version>

# Apply the new image to the deployment
kubectl set image deployment/tessera-api \
  tessera-api=<registry>/tessera-api:<version> \
  -n tessera

# Watch the rollout
kubectl rollout status deployment/tessera-api -n tessera
```

---

## 3. Rollout (config change only, same image)

```bash
kubectl rollout restart deployment/tessera-api -n tessera
kubectl rollout status deployment/tessera-api -n tessera
```

---

## 4. Rolling restart of the judge worker

```bash
kubectl rollout restart deployment/tessera-judge-worker -n tessera
kubectl rollout status deployment/tessera-judge-worker -n tessera
```

The worker has a `terminationGracePeriodSeconds: 120`. In-flight judge jobs are allowed
to complete before the pod is terminated.

---

## 5. Rollback

```bash
kubectl rollout undo deployment/tessera-api -n tessera
kubectl rollout status deployment/tessera-api -n tessera
```

---

## 6. Post-restart verification

```bash
# Health check
curl -sf http://127.0.0.1:8000/health | python -m json.tool
# Expect: {"status": "ok"}

# Smoke test
TOKEN=<test_token>
curl -sf -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "test"}' | python -m json.tool
# Expect: 200 with answer field
```
