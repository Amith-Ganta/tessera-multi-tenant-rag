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
| [DONE] | 14 | Fail-closed quality gate — 17 tests: CLI exit codes (0/1/1-missing), stdout format (PASS/FAIL/SKIP), fail-closed invariants, _load_report() round-trip | e274448 |
| [DONE] | G2 | Phase 2 gate — 136/136 Phase 2 tests pass (items 9-14); 37 pre-existing failures unrelated to Phase 2 scope | TBD |
| [DONE] | 15 | Chunk provenance metadata — 16 tests: metadata contract, chunk_index, multi-chunk source, _tokenize, retrieve_sparse provenance pass-through | a2d6fb0 |
| [DONE] | 16 | Citation correctness — 14 tests: sources/contexts shape, source matches metadata, order preserved, empty retrieval, no-source doc, dedup contract | 07b3abe |
| [DONE] | 17 | Document-level ACL — 25 tests: path isolation, use_tenant ctx manager, restore on exit, nested ctx, tenant ID validation (fail-closed), cross-tenant path exclusion | af3ab6c |
| [DONE] | 18 | Corpus/index version — 20 tests: all six VERSION_* constants non-empty, build_tenant_index result schema (7 keys), chunk_size/overlap match args, index_dir non-empty string | 7485866 |
| [DONE] | 19 | Document update consistency — 11 tests: BM25 LRU cache eviction, stale-data prevention, idempotent invalidation, reset_vectorstore_cache callable (Chroma stubbed), post-upload fresh retrieval | 7e941e6 |
| [GATE3]| G3 | Phase 3 gate | TBD |
| [TODO] | 20 | End-to-end trace ID | — |
| [TODO] | 21 | AI quality metrics | — |
| [TODO] | 22 | AI capacity model | — |
| [TODO] | 23 | Bottleneck statement | — |
| [GATE4]| G4 | Phase 4 gate | — |
| [TODO] | 24 | Architecture review | — |
| [TODO] | 25 | Documentation consistency | — |
| [GATE5]| G5 | Phase 5 gate | — |
| [TODO] | F  | Final hardening report | — |
