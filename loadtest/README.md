# Load Test — Tessera API

## Prerequisites

```bash
uv add --dev locust
```

## Running

```bash
# 50 concurrent users, 5 minutes
uv run locust -f loadtest/locustfile.py --host=http://localhost:8000 \
  --headless -u 50 -r 5 -t 5m --csv=loadtest/results/run_50

# 100 concurrent users, 5 minutes
uv run locust -f loadtest/locustfile.py --host=http://localhost:8000 \
  --headless -u 100 -r 10 -t 5m --csv=loadtest/results/run_100
```

## What to look for

- **p95 < 15 000 ms** at 50 users — hard gate; stop if breached
- **Error rate < 5%** — circuit breaker opens at sustained failures
- **RPS plateau** identifies the throughput ceiling
- **p95 jump between 50 and 100 users** — if it doubles or more, the
  bottleneck is CPU-bound (torch reranker or LLM call); if it stays
  flat, the bottleneck is upstream I/O (OpenRouter latency)
- **Rate limiter 429s** at high user counts are expected and correct

## CSV outputs

Each run writes four files under `loadtest/results/`:

| File | Contents |
|------|----------|
| `run_N_stats.csv` | Per-endpoint aggregated stats (p50/p95/p99, RPS, errors) |
| `run_N_failures.csv` | Per-failure breakdown |
| `run_N_stats_history.csv` | Time-series RPS and response times |
| `run_N_exceptions.csv` | Python exceptions from Locust workers |
