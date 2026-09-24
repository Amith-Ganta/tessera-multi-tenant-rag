# DevOps Baseline Audit — 2026-09-24

Snapshot of the DevOps state **before** the finalization pass.
Captured immediately after the 25-item AI/RAG Hardening Pass completed.

---

## 1. docker-compose.yml

| Service | Healthcheck | depends_on condition |
|---------|-------------|---------------------|
| redis | NONE | n/a |
| api | NONE | `- redis` (no condition) |
| judge-worker | ps-grep for judge_worker.py | `- redis` (no condition) |

**Gaps:**
- Redis has no healthcheck, so Compose cannot gate the API/worker startup on Redis being ready.
- `api` and `judge-worker` both use simple list-form `depends_on`, which only checks container started, not healthy.
- The API has no HTTP healthcheck, so orchestrators cannot verify the app is serving.

---

## 2. k8s/deployment.yaml

### tessera-api container

| Probe | Kind | Path | Port | initialDelaySeconds | periodSeconds | failureThreshold |
|-------|------|------|------|--------------------:|--------------|-----------------|
| liveness | httpGet | /health | 8000 | 15 | 10 | 3 |
| readiness | httpGet | /health | 8000 | 10 | 5 | 2 |
| startup | NONE | — | — | — | — | — |

**Gaps:**
- Liveness and readiness use the same `/health` path with near-identical timing — no meaningful distinction.
- No startupProbe: on a slow cold-start the liveness probe can fire before the app is up and restart the pod.
- Readiness `initialDelaySeconds: 10` is very aggressive for a RAG service that must load models.

### tessera-judge-worker container

| Probe | Kind |
|-------|------|
| liveness | NONE |
| readiness | NONE |
| startup | NONE |

**Gaps:**
- Worker has zero probes. Kubernetes cannot detect a hung or crashed worker.

---

## 3. CI (`.github/workflows/ci.yml`)

| Step | Present |
|------|---------|
| lint (byte-compile) | YES |
| secret scan (truffleHog) | YES |
| redis service for tests | YES |
| regression test suite | YES |
| eval gate | YES |
| dependency vulnerability scan (pip-audit) | NO |

**Gap:** No `pip-audit` step to detect known CVEs in the Python dependency tree.

---

## 4. Summary of planned fixes

| Area | Fix |
|------|-----|
| docker-compose — redis | Add `healthcheck: redis-cli ping` |
| docker-compose — api | Add HTTP healthcheck; change `depends_on` to `condition: service_healthy` |
| docker-compose — judge-worker | Change `depends_on` to `condition: service_healthy` |
| k8s — api liveness | Keep `/health`; increase `initialDelaySeconds` to 30 |
| k8s — api readiness | Use `/health` with tighter timing; add `startupProbe` |
| k8s — api startupProbe | Allow up to 120s of cold-start before handing off to liveness |
| k8s — worker | Add liveness (exec: redis-cli ping), readiness (same), startupProbe |
| CI | Add `pip-audit` step between install and regression tests |
