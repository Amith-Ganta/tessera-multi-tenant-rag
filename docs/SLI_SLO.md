# SLI / SLO / Alerting Contract (DevOps §15)

This document defines the Service Level Indicators (SLIs), proposed
Service Level Objectives (SLOs), alert thresholds, and linked runbooks
for Tessera. Every SLI cites the code or configuration that measures it.
Items with no automated measurement are marked **NOT IMPLEMENTED**.

---

## 1. SLI / SLO Table

### 1.1 Availability

| SLI | Measurement | Proposed SLO | Alert Threshold |
|-----|-------------|--------------|-----------------|
| API availability | Fraction of HTTP requests returning 2xx or 4xx (not 5xx or no response) over a 5-minute window | ≥ 99% over a rolling 24-hour window | < 99% over 5 min → page; < 99.5% over 1 h → warn |
| Health-check liveness | `GET /health` returns 200 within 5 s | 100% during business hours | Any failure → alert |

**Measurement source:**
- Health endpoint: `src/api/app.py` — `/health` route.
- HTTP status codes: FastAPI access log (stdout/stderr); no built-in metric exporter.
- **NOT IMPLEMENTED:** Prometheus metrics endpoint; 5xx rate as a counter.

---

### 1.2 Latency

| SLI | Measurement | Proposed SLO | Alert Threshold |
|-----|-------------|--------------|-----------------|
| `/ask` p95 latency | p95 of end-to-end wall-clock time from request receipt to first byte of response | ≤ 3,000 ms | > 3,000 ms → warn; > 8,000 ms → page |
| `/ask` p99 latency | p99 of end-to-end wall-clock time | ≤ 8,000 ms | > 8,000 ms → page |

**Measurement source:**
- Offline eval baseline: `LATENCY_P95_MAX_MS = 3000.0` ms (`src/rag/config.py:150`).
- Stage-level timing: `src/observability/observability.py` — `time_stage` context manager
  emits per-stage durations (embedding, vector_retrieval, judge).
- Capacity model baseline: p95 async-eval RTT = 3,991 ms (see `docs/CAPACITY_MODEL.md`).
- **NOT IMPLEMENTED:** Real-time p95/p99 metric from production traffic. Current
  measurement is from offline eval only (`evals/gate.py:61`).

---

### 1.3 RAG Quality

| SLI | Measurement | Proposed SLO | Alert Threshold |
|-----|-------------|--------------|-----------------|
| Mean answer relevancy | Average `answer_relevancy` score over evaluated requests in a rolling window | ≥ 0.60 | < 0.60 over 100 evaluated requests → warn |
| Mean correctness | Average `correctness` score over evaluated requests | ≥ 0.50 | < 0.50 over 100 evaluated requests → warn |
| Per-request faithfulness | Fraction of requests whose faithfulness score ≥ 0.70 | ≥ 80% of evaluated requests | < 80% over 50 evaluated requests → warn |

**Measurement source:**
- Offline quality gate thresholds:
  - `MIN_MEAN_RELEVANCY = 0.60` (`src/rag/config.py:148`)
  - `MIN_MEAN_CORRECTNESS = 0.50` (`src/rag/config.py:149`)
  - Gate enforcement: `evals/gate.py:61` — `run_gate(report)`.
- Per-request judge: `src/rag/live_eval.py:75` — `evaluate_answer()` computes
  faithfulness, answer_relevancy, contextual_relevancy, contextual_precision,
  contextual_recall, toxicity, correctness.
- **NOT IMPLEMENTED:** Aggregation of live per-request judge scores into a
  rolling-window quality metric. The per-request scores are produced but not
  persisted or aggregated for alerting.

---

### 1.4 Judge Queue

| SLI | Measurement | Proposed SLO | Alert Threshold |
|-----|-------------|--------------|-----------------|
| Judge queue depth | `LLEN judge:queue` (Redis) | ≤ 100 items at any point | > 80 items → warn; > 100 items → page (shed-load activates at 100) |
| Dead-letter queue (DLQ) depth | `LLEN judge:queue:dlq` (Redis) | = 0 under normal operation | > 0 → warn; > 10 → page |
| Judge worker error rate | Fraction of judge jobs that land in DLQ | < 1% of jobs processed | > 1% → warn |

**Measurement source:**
- Queue depth cap: `JUDGE_QUEUE_MAX_DEPTH = 100` (`src/rag/config.py:114`).
- Shed-load: queue full → HTTP 503 returned to caller (app.py judge submission path).
- DLQ peek: `GET /admin/dlq` returns current DLQ depth.
- Runbook: `docs/runbooks/dlq-drain.md`.
- **NOT IMPLEMENTED:** Automated Redis metric scraping for queue depth alerting.

---

### 1.5 Cost

| SLI | Measurement | Proposed SLO | Alert Threshold |
|-----|-------------|--------------|-----------------|
| Estimated daily LLM spend | `spend_so_far()` accumulated in process memory | ≤ $5.00 per process day | Hard cap at $5.00 — `_check_budget()` refuses LLM calls above cap |
| Cost per request | Estimated cost of a single `/ask` call | ≤ $0.01 | Offline gate: `COST_PER_REQUEST_MAX_USD = 0.01` (`src/rag/config.py:152`) |

**Measurement source:**
- Budget guard: `src/rag/llm.py:121` — `_check_budget()` reads `spend_so_far()`.
- Spend accumulator: `src/rag/llm.py:110` — `record_spend(usd)` adds each call's
  estimated cost to a process-global counter. Resets on process restart.
- Offline gate: `evals/gate.py:61` — cost threshold is one of the five checks.
- Runbook: `docs/runbooks/cost-cap.md`.
- **NOT IMPLEMENTED:** Persistent daily spend tracking across restarts; spend metric
  exported to a monitoring backend.

---

## 2. Alert Routing

| Alert | Severity | Runbook |
|-------|----------|---------|
| API availability < 99% (5 min) | Critical | `docs/runbooks/incident-response.md` |
| `/ask` p95 latency > 8,000 ms | Critical | `docs/runbooks/incident-response.md` |
| Judge queue depth > 100 | Critical | `docs/runbooks/dlq-drain.md` |
| Daily spend cap reached | Critical | `docs/runbooks/cost-cap.md` |
| LLM provider call fails | Warning | `docs/runbooks/llm-provider-failover.md` |
| Redis unavailable | Warning | `docs/runbooks/redis-failover.md` |
| DLQ depth > 0 | Warning | `docs/runbooks/dlq-drain.md` |
| RAG quality degradation (mean relevancy < 0.60) | Warning | `docs/runbooks/incident-response.md` |
| Rolling restart required | Info | `docs/runbooks/rolling-restart.md` |

---

## 3. What Is NOT Automated

| Item | Status |
|------|--------|
| Prometheus / metrics endpoint for real-time SLI collection | NOT IMPLEMENTED — observability is structured logs only |
| Rolling-window aggregation of live judge scores | NOT IMPLEMENTED — per-request scores are produced but not aggregated |
| Automated paging (PagerDuty, Alertmanager, etc.) | NOT IMPLEMENTED — no alerting backend wired |
| Persistent spend accumulator across process restarts | NOT IMPLEMENTED — `record_spend` resets on restart |
| p95/p99 latency measurement from production traffic | NOT IMPLEMENTED — measured from offline eval only |

---

## 4. Relationship to Quality Gate

The offline quality gate (`evals/gate.py`) enforces a subset of the SLOs
above at merge time:

| Gate threshold | Corresponding SLO |
|----------------|------------------|
| `mean_relevancy ≥ 0.60` | RAG quality SLO §1.3 |
| `mean_correctness ≥ 0.50` | RAG quality SLO §1.3 |
| `latency_p95_ms ≤ 3,000` | Latency SLO §1.2 |
| `error_rate ≤ 0.05` | Availability SLO §1.1 |
| `cost_per_request_usd ≤ 0.01` | Cost SLO §1.5 |

The gate blocks a merge but does not monitor production. Production SLOs
require the live instrumentation marked NOT IMPLEMENTED above.
