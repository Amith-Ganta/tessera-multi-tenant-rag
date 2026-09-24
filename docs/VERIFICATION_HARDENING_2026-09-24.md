# Tessera Hardening + DevOps Verification Report
**Date:** 2026-09-24  
**Scope:** 25-item AI/RAG correctness hardening pass + DevOps finalization  
**Verified by:** Independent re-run (read-only — no fixes applied)

---

## Verdict

**PARTIALLY VERIFIED**

All 25 hardening items pass when run by item group. Three tests in
`test_deletion_consistency.py` fail when the full 708-test suite runs together
due to test-ordering / shared-filesystem state. These failures are NOT
pre-existing (the pre-session baseline at commit `67b0808` showed 286 passed,
0 failed) and do NOT indicate a regression in production code; they indicate
missing test isolation in one file. All DevOps artefacts are present and
structurally correct.

---

## Summary Table

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | Items 1–25 pass individually | PASS | 26+55+103+85+156 = 425 tests, all exit 0 |
| 2 | Full suite (708 tests) | 3 FAILURES | `test_deletion_consistency.py` — ordering only |
| 3 | Same 3 tests in isolation | PASS | 5/5 pass, exit 0, 87.82 s |
| 4 | Pre-existing failures (67b0808 baseline) | NONE | 286 passed, 0 failed at pre-session commit |
| 5 | Collection errors | NONE | No import/syntax errors across 56 test files |
| 6 | Hardening log entries | 33 DONE | 0 SKIP, 0 FAIL; all phases covered |
| 7 | All listed commit SHAs exist | PASS | 28 SHAs verified via `git cat-file -t` |
| 8 | Log TBD commit entries | 3 TBD | Items 10 (ed35f75), 11 (cf37391), 13 (35e9619); commits exist |
| 9 | ADR sequence | PASS | ADR-001–ADR-022, no gaps, ADR-022 in df630de |
| 10 | docker-compose healthchecks | PASS | Redis CMD ping, API curl 127.0.0.1, worker ps-grep |
| 11 | docker-compose `condition: service_healthy` | PASS | api + judge-worker both depend on redis healthy |
| 12 | k8s probe separation (liveness ≠ readiness) | PASS | API: 15s liveness / 5s readiness; worker: exec redis-cli |
| 13 | k8s startupProbe present | PASS | API 24×5s=120s budget; worker 12×5s=60s budget |
| 14 | k8s YAML parses | PASS | yaml.safe_load_all() → 2 documents (tessera-api, tessera-judge-worker) |
| 15 | pip-audit in CI | PASS | `uv run pip-audit --strict` in regression-tests job |
| 16 | CHAT_MODEL in docker-compose | PASS | `CHAT_MODEL=gpt-4o-mini`; openai/gpt-4o-mini in RATES registry |
| 17 | DevOps commit (cfb3aa0) | PASS | 5 files changed, 264 insertions |
| 18 | Total commits | 123 | 35 ahead of origin/main, working tree clean |
| 19 | HARDENING_REPORT_2026-09-24.md | PRESENT | 25 items, 5 gates, 171 new tests, 286→457 |

---

## Items Marked DONE Without a Matching Commit

**None.** Every DONE entry in the hardening log has a corresponding commit SHA
that resolves in the repository.

Three entries carry `TBD` in the log's Commit column — this is a documentation
gap, not a missing commit:

| Log ID | TBD entry | Actual commit |
|--------|-----------|---------------|
| 10 | Retrieval metrics | ed35f75 |
| 11 | Failure matrix classification | cf37391 |
| 13 | Shadow promotion methodology | 35e9619 |

---

## Test Failures and Collection Errors

### Failures (3)

All three failures are in `tests/test_deletion_consistency.py` and occur only
when the full 708-test suite runs. The same tests pass in isolation (5/5, 87.82 s).

```
FAILED tests/test_deletion_consistency.py::TestIngestionCreatesArtifacts::test_corpus_file_written_and_index_directory_created
FAILED tests/test_deletion_consistency.py::TestReUploadWipesOldIndex::test_chroma_index_rebuilt_from_scratch_on_reuupload
FAILED tests/test_deletion_consistency.py::TestTenantIsolationOnReUpload::test_other_tenant_index_unaffected_by_first_tenant_rebuild
```

**Root cause:** `test_deletion_consistency.py` writes to and reads from real
filesystem paths (`data/`, `chroma/`). Some of the 422 new hardening tests
write to overlapping paths and leave state that interferes with these three
class-based tests when execution order places them after the polluting tests.

**Pre-existing?** No. The pre-session commit (`67b0808`, 286 tests) shows
0 failures. The failures emerge only when the 422 new tests run before them in
the combined 708-test suite.

**Production impact:** None. Production code is unaffected; this is strictly a
test-suite isolation issue.

### Collection Errors

None. All 56 test files collected without import or syntax errors.

---

## Recommendations

1. **Fill TBD SHAs in hardening log** — update items 10, 11, 13 in
   `docs/HARDENING_LOG_2026-09-24.md` with their confirmed commit SHAs
   (ed35f75, cf37391, 35e9619). Low effort; removes ambiguity.

2. **Fix test isolation in `test_deletion_consistency.py`** — add a
   `tmp_path`-scoped fixture or per-test `chdir` so the three class-based tests
   do not share filesystem state with the wider suite. Running the 708-test suite
   should exit 0.

3. **Push 35 local commits** — the repository is 35 commits ahead of
   `origin/main`. Push when ready to publish.

4. **CI will fail on the ordering issue** — the CI regression gate runs the
   full suite with `-x` (stop on first failure). The ordering failure will block
   CI until recommendation 2 is addressed.
