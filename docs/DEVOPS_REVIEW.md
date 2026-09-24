# DevOps Review — Senior Engineering Questions
**Date:** 2026-09-24  
**Scope:** LOCAL-FIRST — docker-compose, k8s manifests, CI pipeline. AWS/Terraform/EKS out of scope.

---

## Q1. Why does Redis need a healthcheck in docker-compose when `restart: unless-stopped` is already there?

`restart` only restarts a **crashed** container. It does not know whether the process inside is ready to accept connections. Redis takes ~1-2 seconds to initialize its AOF or RDB state. The `api` and `judge-worker` services start in parallel; without `condition: service_healthy`, they may attempt to open the Redis connection pool before Redis is listening on port 6379, causing `ConnectionRefusedError` on startup. The healthcheck (`redis-cli ping`) gives Compose a reliable signal that Redis is truly accepting commands before dependent services start.

---

## Q2. What is the difference between liveness and readiness probes, and why did the original manifest conflate them?

- **Readiness**: "Should this pod receive traffic right now?" — gates the load balancer. A pod that is booting, warming caches, or temporarily overloaded should fail readiness.
- **Liveness**: "Is this pod alive and worth keeping?" — triggers a restart if it fails. It should only fail for genuinely unrecoverable states (deadlock, OOM, infinite loop).

The original manifest used identical `httpGet /health` with near-identical timing for both, which means: (a) the pod gets restarted whenever it is transiently overloaded, causing cascading restarts; (b) a slow-starting pod never gets readiness-gated because the liveness probe fires first and kills it. Separating them fixes both.

---

## Q3. What is startupProbe and why is it critical for a RAG service?

A RAG service loads embedding models, connects to Redis, and may warm a Chroma vector store on startup — this can take 20-60 seconds on first boot. Without a startupProbe, the liveness probe begins polling at `initialDelaySeconds` (previously 15s) and fails after `failureThreshold` × `periodSeconds` = 30s. The pod is killed before it finishes starting. `startupProbe` pauses liveness/readiness checks until startup succeeds, allowing up to `failureThreshold × periodSeconds` = 120s for a cold start, then hands off to the tighter liveness/readiness timing.

---

## Q4. Why does the judge worker need probes at all — it does not serve HTTP traffic?

A worker that cannot reach Redis is silently idle: it dequeues no jobs, but Kubernetes sees a running container and does not restart it. The exec-based `redis-cli ping` probe detects this. A liveness failure restarts the worker; a readiness failure is informational (workers are typically not in a Service). This turns a silent failure into an observable, self-healing one.

---

## Q5. Why use `redis-cli ping` for the worker probe rather than checking the process list?

`ps aux | grep judge_worker.py` only confirms the process is running. The process could be running but stuck in a retry loop waiting for Redis. `redis-cli ping` confirms the worker's actual dependency (Redis) is reachable from inside the container. If Redis goes down and the worker cannot reconnect, the liveness probe fails and the pod is restarted — correct behavior.

---

## Q6. What does `pip-audit --strict` do and why place it before the regression tests?

`pip-audit` scans installed packages against the OSV (Open Source Vulnerabilities) and PyPI Advisory databases. `--strict` exits with code 1 on any confirmed vulnerability, blocking the rest of the job. Placing it before the regression tests makes the pipeline fail-fast: there is no point running a 4-minute test suite if the build is shipping known-vulnerable dependencies. It runs after `uv sync` because it inspects the resolved environment.

---

## Q7. The API healthcheck in docker-compose uses `curl`. What if `curl` is not in the container image?

`curl` must be installed in the Docker image, or the healthcheck fails immediately. The alternative is `wget -q -O - http://127.0.0.1:8000/health || exit 1` (wget is included in Alpine by default), or a Python one-liner: `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"`. The correct fix is to add `curl` to the Dockerfile (`apk add curl` for Alpine, `apt-get install -y curl` for Debian). The healthcheck command must match what is actually available in the image.

---

## Q8. Why is `start_period: 30s` set for the API healthcheck in docker-compose?

Without `start_period`, Compose counts each failed check against `retries` immediately. The FastAPI app takes several seconds to load; its first health response may come 10-15 seconds after the container starts. `start_period: 30s` gives the container a grace window — failures during start_period do not count toward retries, so a slow-starting app is not marked unhealthy on its first check. After the start period, normal `interval`/`retries` logic applies.

---

## Q9. What would happen without `condition: service_healthy` if Redis takes longer than usual to start (e.g. loading a large AOF)?

The `api` container would start, attempt `RateLimiter.__init__()` and `TenantGovernor.__init__()` — both of which open a Redis connection pool — and raise `redis.exceptions.ConnectionError`. With `restart: unless-stopped`, the container would restart, loop crash, and trigger Docker's exponential back-off. The service would appear flapping for 30-60 seconds rather than starting cleanly. `condition: service_healthy` eliminates this entirely by holding the dependent container until Redis passes its healthcheck.

---

## Q10. The k8s readiness probe uses `/health` — what should a purpose-built `/ready` endpoint check differently?

`/health` typically answers "is the process alive?" — usually a trivial 200 OK. A `/ready` endpoint should check actual dependencies: can the app reach Redis, are vector stores initialized, is the rate limiter seeded? If Redis goes read-only or the Chroma index is being rebuilt, the pod should fail readiness (stop receiving traffic) without failing liveness (no restart). Implementing a separate `/ready` endpoint that checks these dependencies is the correct next step; using `/health` for both is a pragmatic intermediate solution.

---

## Q11. What is the significance of `maxUnavailable: 0` in the rolling update strategy?

`maxUnavailable: 0` means zero existing pods may be taken down until a replacement pod is running and healthy (readiness probe passes). This ensures zero-downtime deployments. Combined with `maxSurge: 1`, the deploy creates one new pod, waits for it to become ready, then removes one old pod. The cost is slightly slower rollouts and a brief period of `replicas + 1` pods running concurrently.

---

## Q12. The `terminationGracePeriodSeconds` is 60s for the API and 120s for the worker. Why the difference?

The API handles live HTTP requests — 60s is enough time to drain in-flight requests (FastAPI/uvicorn drains connections on SIGTERM). The judge worker may be mid-evaluation when killed; a 120s grace period allows it to finish the current evaluation cycle before the process receives SIGKILL. Setting this too low would cause partially-written judge results in the store.

---

## Q13. Why does the CI `regression-tests` job use `redis://127.0.0.1:6379` rather than `redis://localhost:6379`?

`localhost` resolves to `::1` (IPv6 loopback) on some GitHub Actions runner configurations, while Redis binds to `127.0.0.1` (IPv4) by default. Using `127.0.0.1` explicitly avoids an IPv6/IPv4 mismatch that causes `ConnectionRefusedError` in CI even though Redis is running. This matches the project's standing constraint: always use `127.0.0.1`, never `localhost`.

---

## Q14. The truffleHog secret scan uses `--only-verified`. What does that mean and is it sufficient?

`--only-verified` instructs truffleHog to only report secrets it can confirm are active (e.g., by calling the API with the key). This reduces false positives but also means a committed key that has already been rotated would not be flagged. For defense-in-depth, `--only-verified` should be paired with `--no-update` (prevent auto-updating the scanner itself from CI) and a separate static scan (e.g., `detect-secrets` pre-commit hook) that catches patterns regardless of validity.

---

## Q15. The judge worker image is `tessera-api:latest`. What is the risk of using `latest` in k8s?

`latest` is not a specific version — two pods may pull different image layers if a new push occurs mid-deploy. `imagePullPolicy: IfNotPresent` mitigates this on nodes that already have the image, but a newly scaled node will pull the newest `latest`. The correct practice is to tag images with the git SHA (`tessera-api:abc1234`) and update the manifest in CI. This ensures rollouts are reproducible and rollbacks are exact.

---

## Q16. Why does pip-audit run inside the `regression-tests` job rather than in the `lint` job?

`lint` runs `uv sync --dev` and installs the full environment, so pip-audit could run there. However, placing it in `regression-tests` makes the dependency check part of the same job that runs the tests, ensuring that any vulnerability report is co-located with the test results artifact. An alternative is a dedicated `security` job that runs in parallel with both `lint` and `regression-tests`. Either approach is valid; the key constraint is that it blocks `eval-gate` via the `needs` chain.

---

## Q17. What is the overall startup ordering contract after these DevOps fixes?

```
docker-compose (local):
  redis starts → healthcheck passes (redis-cli ping) →
  api starts   → healthcheck passes (/health HTTP 200) →
  judge-worker starts

k8s (cluster):
  tessera-api pod:
    startupProbe (up to 120s) → passes →
    liveness + readiness probes begin polling →
    readiness passes → pod added to Service endpoints

  tessera-judge-worker pod:
    startupProbe (redis-cli ping, up to 60s) → passes →
    liveness + readiness probes begin polling
```

This gives every component a deterministic startup gate, turns silent failures into observable events, and enables self-healing without manual intervention.
