# Tessera -- Project Closure Log
**Date:** 2026-09-24
**Session start HEAD:** cfb3aa0b9d091a4ff2f107ec69285a0a4b06cdb1

---

## Task 0 -- State at Session Start

```
HEAD:        cfb3aa0 (devops: healthchecks, probe separation, pip-audit, baseline + review docs)
Unpushed:    35 commits ahead of origin/main
Test suite:  3 failed, 708 passed (exit 1)
Failures:    tests/test_deletion_consistency.py
  - TestIngestionCreatesArtifacts::test_corpus_file_written_and_index_directory_created
  - TestReUploadWipesOldIndex::test_chroma_index_rebuilt_from_scratch_on_reuupload
  - TestTenantIsolationOnReUpload::test_other_tenant_index_unaffected_by_first_tenant_rebuild
Untracked:   docs/VERIFICATION_HARDENING_2026-09-24.md
```

---

## Checklist

- [x] **Task 1** -- Fix test isolation (BLOCKER) -- gate: full suite green (commit 45aa40d)
- [x] **Task 2** -- DevOps section 10: AI-specific CI/CD trigger (commit 039c31b)
- [x] **Task 3** -- DevOps section 12: Model/prompt release safety lifecycle (commit c4eaa8b)
- [x] **Task 4** -- DevOps section 13: Embedding/index migration strategy (commit b51c56c)
- [x] **Task 5** -- DevOps section 15: SLI/SLO/alerting contract (commit a8dab1b)
- [ ] **Task 6** -- DevOps section 23: Local end-to-end Docker verification (IN PROGRESS)
- [ ] **Task 7** -- Final push + closure report

---

## Task Log

### Task 1 -- Fix Test Isolation

**Status:** DONE
**Commit:** 45aa40d
**Root cause:** Multiple test files inject a sys.modules stub for langchain_chroma via
sys.modules.setdefault() at import time. When pytest collects those modules before
test_deletion_consistency.py, the stub stays in sys.modules and any subsequent
"from langchain_chroma import Chroma" returns MagicMock instead of the real class.
Chroma.from_documents then writes nothing to disk, causing the index_dir.rglob("*")
assertion to fail.

**Fix:** Added autouse fixture _use_real_langchain_chroma to
tests/test_deletion_consistency.py. The fixture saves sys.modules["langchain_chroma"],
pops the stub, force-imports the real package, yields, then restores the original state.

**Gate 1 result:** 711 passed, 0 failed (full suite, all test files).

---

### Task 2 -- DevOps section 10

**Status:** DONE
**Commit:** 039c31b
**Files created:**
- .github/workflows/ai-eval.yml -- dedicated workflow triggered on AI-affecting path changes
- docs/AI_CI.md -- documents triggering paths, what the gate runs, what blocks a merge

**Gate 2 result:** yaml.safe_load validates both ci.yml and ai-eval.yml; jobs listed:
  ci.yml: ['lint', 'regression-tests', 'eval-gate']
  ai-eval.yml: ['ai-eval-gate']

---

### Task 3 -- DevOps section 12

**Status:** DONE
**Commit:** c4eaa8b
**File created:** docs/MODEL_RELEASE_LIFECYCLE.md
**Covers:** candidate registration, offline eval, shadow experiment, promotion gate,
canary rollout, monitoring, rollback. Every step cites file:line. NOT IMPLEMENTED items
are explicitly marked.

**Gate 3 result:** All 10 cited files verified to exist on disk.

---

### Task 4 -- DevOps section 13

**Status:** DONE
**Commit:** b51c56c
**File created:** docs/INDEX_MIGRATION.md
**Covers:** breaking vs compatible changes, build-new-never-overwrite principle,
migration procedure, shadow compare, atomic switch limitation, rollback, corruption
recovery, version identifiers in responses. Every step cites file:line.

**Gate 4 result:** All 10 cited files verified to exist on disk.

---

### Task 5 -- DevOps section 15

**Status:** DONE
**Commit:** a8dab1b
**File created:** docs/SLI_SLO.md
**Covers:** Availability, Latency, RAG quality, Judge queue, and Cost SLIs with
proposed SLOs and alert thresholds. Every threshold cites its config constant.
NOT IMPLEMENTED items (Prometheus endpoint, rolling-window quality aggregation,
paging backend) are explicitly marked. Runbook links in alert routing table.

**Gate 5 result:** All 11 cited files verified to exist on disk.

---

### Task 6 -- DevOps section 23

**Status:** IN PROGRESS

**Step 1 -- docker compose config (PASSED)**

Command:
```
docker compose config
```
Result: PASSED -- compose file parses correctly, all three services (redis, api,
judge-worker) resolve. One non-fatal warning: `version` attribute is obsolete in
Compose spec v3.9. Secrets redacted from log per project standing constraint.

**Step 2 -- docker build (IN PROGRESS)**

Command:
```
docker build -f deploy/Dockerfile -t tessera-api:closure-test .
```
Build started at approx 23:01 local time. The Dockerfile comment states cold
dependency install takes ~16 min on this machine (torch + deepeval). Awaiting
completion notification.

---

### Task 7 -- Final Push

**Status:** PENDING
