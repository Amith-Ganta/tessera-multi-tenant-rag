# Load Tests — Tessera Multi-Tenant RAG API

## Quick start

```bash
pip install locust
locust -f loadtests/locustfile.py --host http://127.0.0.1:8000
```

Open [http://127.0.0.1:8089](http://127.0.0.1:8089) to configure and run.

## Baseline capacity target

| Metric | Target |
|---|---|
| Virtual users | 50 |
| Spawn rate | 5 / s |
| p50 latency (`/ask`) | < 2 s |
| p99 latency (`/ask`) | < 8 s |
| Error rate | < 1 % |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TESSERA_LOAD_USER` | `loadtest_user` | Username prefix for simulated users |
| `TESSERA_LOAD_PASS` | `loadtest_pass` | Password for simulated users |
| `TESSERA_ADMIN_PASS` | `admin_pass` | Password for the `admin` account |

## Task mix

| Endpoint | Weight | Notes |
|---|---|---|
| `POST /ask` (adaptive) | 8 | Main query path |
| `POST /ask` (dense) | 2 | Dense-only strategy |
| `GET /health` | 1 | Liveness check |
| `GET /metrics` | 1 | Prometheus scrape simulation |
| `GET /admin/dlq` | 1 (admin only) | DLQ depth monitoring under load |

## Headless CI run

```bash
locust -f loadtests/locustfile.py --host http://127.0.0.1:8000 \
  --users 50 --spawn-rate 5 --run-time 60s --headless \
  --csv loadtests/results/baseline
```

Results are written to `loadtests/results/baseline_*.csv`.
Do not commit result CSV files (they are in .gitignore).
