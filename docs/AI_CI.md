# AI-Affecting CI/CD Gate (DevOps §10)

## Purpose

Changes that affect AI behaviour (prompts, model selection, embeddings,
chunking, retrieval, reranking, context selection, generation parameters,
or evaluation configuration) must pass the quality gate before they can
be merged to `main`. This document defines which paths trigger the gate,
what the gate runs, and what blocks a merge.

---

## Triggering Paths

The `.github/workflows/ai-eval.yml` workflow fires on push or pull
request to `main` when any of the following files change:

| Path | AI Concern |
|------|-----------|
| `src/rag/generator.py` | Prompt templates and generation config |
| `src/rag/llm.py` | Model routing, canary selection, fallback |
| `src/rag/embedding*.py` | Embedding model and dimension |
| `src/rag/ingest.py` | Chunking strategy and overlap |
| `src/rag/retriever_dense.py` | Dense (vector) retrieval |
| `src/rag/retriever_sparse.py` | Sparse (BM25) retrieval |
| `src/rag/reranker.py` | Reranker model and threshold |
| `src/rag/strategies.py` | Context selection strategy |
| `evals/**` | Golden dataset, eval harness, gate config |

Changes to any other path (tests, docs, CI config, runbooks, etc.) do
not trigger this workflow.

---

## What the Job Runs

1. **`uv run python -m evals.run_eval`**  
   Runs the evaluation harness (`evals/run_eval.py`). Reads the golden
   dataset from `evals/golden/`, calls the RAG pipeline on each entry
   with the judge model (configured via `DEEPSEEK_API_KEY` /
   `OPENAI_API_KEY`), and writes results to `evals/reports/latest.json`.

2. **`uv run python -m evals.gate`**  
   Reads `evals/reports/latest.json` and checks that all quality
   thresholds pass. Exits 0 on pass, exits 1 on any failure. The gate
   is fail-closed: a missing or empty report counts as a failure.

---

## What the Gate Checks

The gate (`evals/gate.py`) enforces the quality thresholds defined in
`evals/` configuration. A run fails if:

- Any golden dataset entry fails its expected-answer assertion, OR
- The aggregate pass-rate falls below the configured threshold, OR
- The report is absent or malformed.

See `evals/gate.py` for the exact thresholds.

---

## What Blocks a Merge

The `ai-eval-gate` job is configured as a required status check. A pull
request cannot be merged to `main` if:

- `evals.gate` exits non-zero (quality regression detected), OR
- `DEEPSEEK_API_KEY` or `OPENAI_API_KEY` secrets are absent.

The main CI `eval-gate` job (`.github/workflows/ci.yml`) also runs the
same gate on every push/PR regardless of which paths changed, providing
a second layer of protection.

---

## Relationship to the Main CI Workflow

| Workflow | Trigger | Scope |
|----------|---------|-------|
| `.github/workflows/ci.yml` | All changes to `main` | Lint + regression tests + eval gate |
| `.github/workflows/ai-eval.yml` | AI-affecting paths only | Eval gate only (faster, targeted) |

Both workflows must pass for a PR touching AI paths to be merged. The
dedicated `ai-eval.yml` workflow ensures the gate is explicitly visible
in the PR check list for AI changes, even when the main CI is bypassed
or takes a different code path.
