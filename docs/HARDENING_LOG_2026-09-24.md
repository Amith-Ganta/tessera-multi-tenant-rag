# Hardening Log — 2026-09-24

## ADR Baseline: ADR-021 (next: ADR-022)
## Test Baseline: 286

| Status | ID | Artifact | Commit |
|--------|----|----------|--------|
| [DONE] | 1  | A2A tenant authorization | df630de |
| [DONE] | 2  | Stale BM25 cache | b744d73 |
| [DONE] | 3  | Vectorstore cache + index rebuild | 2840d12 |
| [DONE] | 4  | Semantic cache correctness | 5c7f577 |
| [DONE] | 5  | Judge feedback injection | 4bcafed |
| [DONE] | 6  | Canonical LLM gateway | 7af8ccf |
| [DONE] | 7  | Streaming semantics | 9be5bfe |
| [DONE] | 8  | Canary model versioning | 6c172fb |
| [DONE] | G1 | Phase 1 gate — 327 tests pass (exit 0) | 94728ac |
| [DONE] | 9  | Expand golden dataset — 12→20 entries, 16 schema/negative/coverage tests | 77c08ec |
| [DONE] | 10 | Retrieval metrics — 24 unit+integration tests for P@k/R@k/aggregates | TBD |
| [DONE] | 11 | Failure matrix classification — 27 tests: CB/bulkhead/rate-limit/queue | TBD |
| [DONE] | 12 | Judge limitations — 21 edge-case tests: gate boundaries, skip-on-absent, quality signal, submit_judge MM-02 path | f84487f |
| [DONE] | 13 | Shadow promotion methodology — 22 tests: ShadowResult accumulation, compare() fail-closed (samples/absent metric/margin), exact boundary, negative margin, run_shadow_experiment() | TBD |
| [TODO] | 14 | Fail-closed quality gate | — |
| [GATE2]| G2 | Phase 2 gate | — |
| [TODO] | 15 | Chunk provenance metadata | — |
| [TODO] | 16 | Citation correctness | — |
| [TODO] | 17 | Document-level ACL | — |
| [TODO] | 18 | Corpus/index version | — |
| [TODO] | 19 | Document update consistency | — |
| [GATE3]| G3 | Phase 3 gate | — |
| [TODO] | 20 | End-to-end trace ID | — |
| [TODO] | 21 | AI quality metrics | — |
| [TODO] | 22 | AI capacity model | — |
| [TODO] | 23 | Bottleneck statement | — |
| [GATE4]| G4 | Phase 4 gate | — |
| [TODO] | 24 | Architecture review | — |
| [TODO] | 25 | Documentation consistency | — |
| [GATE5]| G5 | Phase 5 gate | — |
| [TODO] | F  | Final hardening report | — |
