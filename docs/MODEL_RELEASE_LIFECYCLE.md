# Model and Prompt Release Lifecycle (DevOps §12)

This document describes the lifecycle for safely releasing a new model, prompt
template, or generation configuration change in Tessera. Each step cites the
file and line that implements it, or is marked **NOT IMPLEMENTED** where no
code exists.

---

## Lifecycle Overview

```
candidate → offline eval → shadow / canary → production → monitor → rollback
```

---

## Step 1 — Register a Candidate

A candidate is a new model name, prompt variant, or retrieval configuration.
It is not activated; it is registered for evaluation only.

**Implementation:**

- Model names are registered in `src/rag/models.py` — the `MODEL_REGISTRY`
  dict. A new model is added as an entry with `litellm_id` and `api_key_env`.
- The canary model version is configured via environment variables
  `MODEL_CANARY_VERSION` and `MODEL_CANARY_PERCENT`, read at startup in
  `src/rag/config.py`.
- **The candidate is not auto-promoted.** Registering a model name or setting
  `MODEL_CANARY_VERSION=new-model` in config is a manual, deliberate act.

---

## Step 2 — Offline Evaluation Against Goldens

The candidate is evaluated against the golden dataset before any live traffic.

**Implementation:**

- Golden dataset: `goldens/retriever_goldens.json` — loaded at runtime via
  `GOLDENS_PATH` from `src/rag/config.py`. Contains input queries, expected
  answers, and quality metadata.
- Eval harness: `evals/run_eval.py` — runs all golden entries through the RAG
  pipeline and writes results to `evals/reports/latest.json`.
- Quality gate: `evals/gate.py:61` — `run_gate(report)` checks five thresholds:
  - `mean_relevancy ≥ MIN_MEAN_RELEVANCY` (`src/rag/config.py`)
  - `mean_correctness ≥ MIN_MEAN_CORRECTNESS` (`src/rag/config.py`)
  - `latency_p95_ms ≤ LATENCY_P95_MAX_MS` (`src/rag/config.py`)
  - `error_rate ≤ ERROR_RATE_MAX` (`src/rag/config.py`)
  - `cost_per_request_usd ≤ COST_PER_REQUEST_MAX_USD` (`src/rag/config.py`)
- Gate exit codes: 0 = pass, 1 = fail or missing report (`evals/gate.py:96`).
- CI enforcement: `.github/workflows/ci.yml` runs the gate as a required step.
  `.github/workflows/ai-eval.yml` re-runs it when AI-affecting paths change.

**Fail-closed:** A missing report returns exit code 1 (`evals/gate.py:99`).
Promotion is blocked if the gate fails.

---

## Step 3 — Shadow Evaluation on Sampled Queries

If offline eval passes, the candidate can be run in shadow mode alongside the
current baseline on sampled representative queries (not live user traffic).

**Implementation:**

- `src/rag/promotion_gate.py:147` — `run_shadow_experiment(queries, baseline_fn,
  candidate_fn)` executes both functions over the query set, tolerates
  per-sample exceptions, and aggregates scores into `ShadowResult` objects.
- `src/rag/promotion_gate.py:36` — `ShadowResult.record(metrics)` accumulates
  per-metric scores.
- Minimum samples: `MIN_SHADOW_SAMPLES = 5` (`promotion_gate.py:23`). Fewer
  samples → `promote=False` (fail-closed).

**NOT IMPLEMENTED:** Automatic sampling of live traffic for shadow queries.
The `run_shadow_experiment` function is available but must be invoked manually
with a pre-prepared query set. There is no middleware that intercepts live
requests and routes copies to the candidate.

---

## Step 4 — Promotion Gate: Paired Comparison

A statistical comparison of baseline vs. candidate is performed before any
production exposure.

**Implementation:**

- `src/rag/promotion_gate.py:75` — `compare(baseline, candidate, primary_metrics,
  margin)` checks that the candidate mean score meets or exceeds the baseline
  mean score plus `PROMOTION_MARGIN` on every primary metric.
- `PROMOTION_MARGIN = 0.02` — candidate must be at least 2% better on each
  primary metric (`promotion_gate.py:28`).
- `PRIMARY_METRICS = ("faithfulness", "answer_relevancy")` (`promotion_gate.py:32`).
- All three fail-closed conditions (per ADR-019):
  - `baseline.sample_count < MIN_SHADOW_SAMPLES` → `promote=False`
  - Primary metric absent from either run → `promote=False`
  - `candidate_mean < baseline_mean + margin` on any metric → `promote=False`
- Returns `PromotionDecision` (`promotion_gate.py:65`) with `promote: bool`,
  `reason: str`, per-metric `checks`, and summaries.

**NOT IMPLEMENTED:** The gate is not wired into a CI step or deployment
pipeline automatically. It must be invoked manually (see ADR-019, Consequences).

---

## Step 5 — Production Rollout via Canary

If the promotion gate passes, the candidate is exposed to a controlled
percentage of live traffic via the canary mechanism.

**Implementation:**

- `src/rag/llm.py:41` — `_select_model(default_model)` hashes the active
  tenant ID deterministically and returns `MODEL_CANARY_VERSION` if the hash
  falls below `MODEL_CANARY_PERCENT`.
- Config: `MODEL_CANARY_VERSION` and `MODEL_CANARY_PERCENT` are read from
  environment at startup (`src/rag/config.py`).
- System-level calls (no active tenant context) are never canaried
  (`src/rag/llm.py:47` — `tid is None` guard).
- Canary rollout is gradual: set `MODEL_CANARY_PERCENT=5` to expose 5% of
  tenants, increase incrementally after observing metrics.

---

## Step 6 — Monitoring During Rollout

Live quality signals are emitted during production rollout.

**Implementation:**

- `src/rag/live_eval.py` — `emit_quality_signal(trace_id, metrics)` sends
  per-request quality metrics (faithfulness, answer_relevancy, context_recall)
  to the judge queue.
- `src/resilience/tenant_governance.py` — `TenantGovernor` tracks per-tenant
  request counts and enforces daily caps.
- Cost monitoring: `src/rag/llm.py:110` — `record_spend(usd)` accumulates
  process-wide estimated spend; `DAILY_SPEND_USD_CAP` enforces a hard ceiling
  (`src/rag/llm.py:121`).
- Observability spans: `src/rag/observability.py` — `trace_llm` context
  manager emits model name, token usage, and estimated cost.

**NOT IMPLEMENTED:** A dedicated rollout dashboard or alert rule that fires
specifically during canary transitions. Monitoring relies on the general
observability signals described above.

---

## Step 7 — Rollback Procedure

Rollback is a config revert, not a code revert.

**Procedure:**

1. Set `MODEL_CANARY_PERCENT=0` (or unset `MODEL_CANARY_VERSION`) in the
   deployment environment config.
2. Redeploy / restart the API containers. No code change is required.
3. `_select_model` will return the default model for all requests
   (`src/rag/llm.py:45` — early return if `MODEL_CANARY_PERCENT <= 0`).
4. Confirm quality signals return to baseline levels.

**For a prompt-only rollback:** Revert the change in `src/rag/generator.py`
and redeploy. The same quality gate in CI blocks the bad prompt from reaching
`main`.

---

## What Is NOT Automated

| Item | Status |
|------|--------|
| Auto-promotion from shadow eval to canary | NOT IMPLEMENTED — manual step |
| Live traffic sampling for shadow queries | NOT IMPLEMENTED — manual query set |
| Canary percentage auto-increase on green metrics | NOT IMPLEMENTED — manual config |
| Promotion gate wired into CI/deployment pipeline | NOT IMPLEMENTED — manual invocation |
| Automatic rollback on quality regression | NOT IMPLEMENTED — manual config revert |

---

## Summary: File-to-Step Map

| Step | File | Key Lines |
|------|------|-----------|
| Register candidate | `src/rag/models.py`, `src/rag/config.py` | MODEL_REGISTRY, MODEL_CANARY_VERSION |
| Offline eval | `evals/run_eval.py`, `evals/gate.py` | run_gate():61, main():96 |
| Shadow experiment | `src/rag/promotion_gate.py` | run_shadow_experiment():147 |
| Promotion gate | `src/rag/promotion_gate.py` | compare():75, PROMOTION_MARGIN:28 |
| Canary rollout | `src/rag/llm.py` | _select_model():41, _select_model:45 |
| Monitoring | `src/rag/live_eval.py`, `src/rag/llm.py` | emit_quality_signal, record_spend:110 |
| Rollback | `src/rag/llm.py` | _select_model:45 (early return) |
