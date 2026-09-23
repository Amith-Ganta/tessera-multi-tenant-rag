# Cost Model

**Tessera Multi-Tenant RAG API**
Last updated: 2026-09-24

---

## 1. Per-Model Token Rates

Blended USD per 1 000 000 tokens (prompt + completion combined).
Rates are estimates for the spend guard and analytics, not billed figures.
Source of truth: `src/observability/cost.py:RATES`.

| Model (LiteLLM ID) | USD / 1M tokens | Notes |
|---|---|---|
| `deepseek/deepseek-flash` | 0.25 | Default model; ~5:1 input:output ratio assumed |
| `deepseek/deepseek-chat` | 0.27 | Legacy alias; same provider |
| `openai/gpt-4o-mini` | 0.15 | Fallback when DeepSeek is down |
| `openai/gpt-4o` | 2.50 | Reserved for high-complexity routes (not default) |

Update both this table and `src/observability/cost.py:RATES` when provider pricing changes.

---

## 2. Per-Request Cost Breakdown

A typical `/ask` request with `run_eval=True` and `JUDGE_MODE=async`:

| Sub-call | Tokens (est.) | Cost (est.) |
|---|---|---|
| Dense retrieval embedding (OpenAI) | 20 prompt | $0.000000 (embedding model separate billing) |
| LLM generation (deepseek-flash) | 800 prompt + 250 completion | $0.000263 |
| Async judge job (deepseek-flash, per metric) | 600 prompt + 100 completion | $0.000175 × 8.5 metrics = $0.001488 |
| **Total per request** | ~9,500 tokens | **~$0.001751** |

Source: capacity model measurements at `docs/capacity-model.md`.

---

## 3. Daily Spend Cap

The in-process spend accumulator (`src/observability/cost.py`) enforces a hard daily
cap per replica process.

| Parameter | Default | Env var | Effect when exceeded |
|---|---|---|---|
| `TESSERA_DAILY_SPEND_USD_CAP` | 5.00 USD | Yes | LLM calls refused with HTTP 402 |

Set to `0` to disable.

The cap is **per-replica** and resets on process restart. For cross-replica budgeting,
aggregate `estimated_cost_usd` from `logs/analytics.jsonl`.

---

## 4. Cost at Scale

Using baseline measurements from `docs/capacity-model.md`:

| Daily requests | Avg RPS | Est. daily cost (full eval) | Est. daily cost (no eval) |
|---|---|---|---|
| 10 000 | 0.116 | $17.51 | $2.63 |
| 50 000 | 0.579 | $87.55 | $13.15 |
| 100 000 | 1.157 | $175.10 | $26.30 |

Set `TESSERA_DAILY_SPEND_USD_CAP` at 110% of the day's expected budget as a safety net.

---

## 5. Embedding Cost (OpenAI)

Embedding calls use `text-embedding-3-small`.

| Parameter | Value |
|---|---|
| Rate | $0.020 / 1M tokens |
| Tokens per query | ~20 tokens |
| Cost per query | $0.0000004 |

Embedding cost is tracked in `app.py:OPENAI_TEXT_EMBEDDING_3_SMALL_USD_PER_1M_TOKENS_ESTIMATED`
but not yet included in `estimated_cost_usd` — only the generation step is tracked.
This is documented as a known gap.

---

## 6. Open Items

| Gap | Detail |
|---|---|
| Embedding cost not in estimated_cost_usd | Generation cost only; embedding adds ~0.02% |
| Cross-replica aggregation | Each replica tracks independently; no shared counter |
| Judge cost not separated | Judge tokens folded into total; no per-call breakdown |
