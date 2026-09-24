# Hardening Log — 2026-09-24

## ADR Baseline: ADR-021 (next: ADR-022)
## Test Baseline: 286

| Status | ID | Artifact | Commit |
|--------|----|----------|--------|
| [DONE] | 1  | A2A tenant authorization | df630de |
| [DONE] | 2  | Stale BM25 cache | b744d73 |
| [DONE] | 3  | Vectorstore cache + index rebuild | 2840d12 |
| [DONE] | 4  | Semantic cache correctness | 5c7f577 |
| [TODO] | 5  | Judge feedback injection | — |
| [TODO] | 6  | Canonical LLM gateway | — |
| [TODO] | 7  | Streaming semantics | — |
| [TODO] | 8  | Canary model versioning | — |
| [GATE1]| G1 | Phase 1 gate | — |
| [TODO] | 9  | Expand golden dataset | — |
| [TODO] | 10 | Retrieval metrics | — |
| [TODO] | 11 | Failure matrix classification | — |
| [TODO] | 12 | Judge limitations | — |
| [TODO] | 13 | Shadow promotion methodology | — |
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
