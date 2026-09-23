# Retrieval Experiment Framework

This directory contains the tooling for running controlled retrieval experiments
without touching the production API or making real embedding calls.

## Structure

```
experiments/
  config.py          – ExperimentConfig dataclass (serialisable to/from JSON)
  runner.py          – run_experiment() function; accepts mock embedder for offline use
  baselines/
    baseline.json    – Default production config
    high-recall.json – Wide-net config (top_k=10, lower rerank threshold)
    low-latency.json – Narrow config (top_k=3, high rerank skip)
```

## Running an experiment

```python
from experiments.config import ExperimentConfig
from experiments.runner import run_experiment

config = ExperimentConfig.from_file("experiments/baselines/baseline.json")

# Provide a mock embedder and retriever for offline testing
config.embedder_fn = lambda text: [0.1] * 1536

def mock_retrieve(embedding, top_k, metric):
    return ["doc-1", "doc-2", "doc-3"][:top_k]

cases = [
    ("What is least privilege?", ["doc-1"]),
    ("How does encryption at rest work?", ["doc-2"]),
]

result = run_experiment(config, cases, mock_retrieve)
print(result["aggregates"])
```

## Adding a new baseline

1. Create a JSON file under `experiments/baselines/` using the schema from `config.py`.
2. Load it with `ExperimentConfig.from_file(path)`.
3. Run `run_experiment()` with your retriever and evaluate the aggregates.

## Design notes

- `ExperimentConfig.embedder_fn` is **not** serialised to JSON; it is injected at
  runtime to keep baseline files environment-agnostic.
- `run_experiment()` raises `RuntimeError` if no `embedder_fn` is set and no
  production embedder is configured, preventing silent real-API calls.
- All measurements are per-query (precision@k, recall@k, latency_ms) plus
  aggregated means.
