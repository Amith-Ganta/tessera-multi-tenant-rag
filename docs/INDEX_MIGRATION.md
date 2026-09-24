# Embedding and Index Migration Strategy (DevOps §13)

This document describes how Tessera handles changes to embedding models,
chunk parameters, or retrieval configuration — collectively called index
migrations. Each section cites the file and line that implements the
described behaviour, or is marked **NOT IMPLEMENTED** where no code
exists.

---

## 1. What Makes a Change Breaking vs Compatible

### Breaking changes (require full index rebuild for every tenant)

| Change | Why it breaks the index |
|--------|------------------------|
| Embedding model (`EMBEDDING_MODEL` in `src/rag/config.py:59`) | Vectors from model A are geometrically incompatible with vectors from model B. A mixed-model index returns meaningless similarity scores. |
| Vector dimension (implied by model change) | Chroma column width is fixed at collection creation. Mismatched dimension = silent wrong results or exception. |
| `CHUNK_SIZE` (`src/rag/config.py:54`) | Chunk boundaries change. The same document produces different chunks, so pre-change embeddings no longer map to post-change text. |
| `CHUNK_OVERLAP` (`src/rag/config.py:55`) | Same reason as CHUNK_SIZE. |
| Text-splitter type or parameters | Same reason as CHUNK_SIZE. |

### Compatible changes (no index rebuild required)

| Change | Why it is safe |
|--------|---------------|
| Top-K retrieval count (`RETRIEVER_TOP_K`) | Read at query time from `src/rag/config.py`; index is unchanged. |
| Reranker model or threshold (`src/rag/reranker.py`) | Reranker operates post-retrieval on results returned from the index. |
| LLM model, prompt template (`src/rag/generator.py`) | Generator is downstream of retrieval; index not involved. |
| Adding new documents (upload) | `build_tenant_index` already rebuilds from scratch — no migration needed. |
| Removing documents (`DELETE /documents/{filename}`) | Same rebuild path. |

---

## 2. Current Index Layout

```
data/
  index/
    tenants/
      <tenant_slug>/
        chroma/           # Chroma SQLite files (collection_uuid.sqlite3, etc.)
  tenants/
    <tenant_slug>/
      corpus/             # Raw documents (.md, .txt)
```

- Index path per tenant: `tenant_index_dir(tenant_id)` — `src/rag/tenant_context.py`
- Index is rebuilt in `build_tenant_index` (`src/rag/ingest.py:91`) via `shutil.rmtree` then
  `Chroma.from_documents`. The build is always build-new-never-overwrite:
  `src/rag/ingest.py:114–116`.
- The vectorstore cache (`_VECTORSTORE_CACHE` in `src/rag/retriever_dense.py:42`) is
  per-index-directory. After a rebuild, `reset_vectorstore_cache()` (`retriever_dense.py:82`)
  and `invalidate_bm25_cache()` (`retriever_sparse.py:45`) evict stale entries.

---

## 3. Migration Procedure for Breaking Changes

### Build-New-Never-Overwrite Principle

Tessera's `build_tenant_index` already implements this for every upload:
`shutil.rmtree(index_dir)` followed by `Chroma.from_documents` in
`src/rag/ingest.py:114–124`. The pattern applies equally to a planned
migration.

### Step-by-Step for a Breaking Change

1. **Update configuration** — change `EMBEDDING_MODEL`, `CHUNK_SIZE`, or
   `CHUNK_OVERLAP` in environment variables or `src/rag/config.py`.

2. **Trigger a rebuild for every tenant** — call `POST /upload` with an
   existing corpus document (or trigger `build_tenant_index` directly) for
   each tenant. There is no bulk-rebuild endpoint; rebuilds are per-tenant.
   - **NOT IMPLEMENTED:** a `/admin/rebuild-all` endpoint.

3. **Verify the rebuilt index** — query a known document via `/ask` and
   confirm the returned sources match expected content.

4. **Evict caches** — `reset_vectorstore_cache()` is called automatically
   by `build_tenant_index` (`retriever_dense.py:82` via ingest.py:146).
   `invalidate_bm25_cache()` is also called automatically (ingest.py:142).
   No manual cache flush is needed if a rebuild is triggered via the upload
   endpoint.

5. **Update version identifiers** — `VERSION_EMBEDDING` in
   `src/rag/config.py:142` is derived from `EMBEDDING_MODEL`. Every `/ask`
   response includes `index_version` (from `VERSION_EMBEDDING` and
   `VERSION_RETRIEVAL`) so callers can detect when an index was rebuilt with
   a different configuration. Update `VERSION_PROMPT` or `VERSION_EVAL_DATASET`
   as appropriate for the change.

---

## 4. Shadow Compare Before Full Rollout

For high-risk changes (embedding model upgrade, major chunk-size change):

1. **Build a shadow index** in a separate directory using the new settings.
   **NOT IMPLEMENTED:** no automated shadow-index path. Manual procedure:
   - Point a test instance at the new parameters via env vars.
   - Upload a representative sample corpus.
   - Measure retrieval quality against the golden dataset
     (`goldens/retriever_goldens.json`) using `evals/run_eval.py`.

2. **Compare** the shadow quality report against the baseline using
   `src/rag/promotion_gate.py:75` (`compare()`) with
   `PROMOTION_MARGIN = 0.02` (`promotion_gate.py:28`).

3. Only proceed with the full per-tenant rebuild if the shadow eval passes
   the promotion gate.

---

## 5. Atomic Switch

At the file-system level the switch is atomic per tenant:

1. Old index directory is deleted: `shutil.rmtree(index_dir)` at
   `src/rag/ingest.py:115`.
2. New index is written to the same path: `Chroma.from_documents` at
   `src/rag/ingest.py:120–124`.
3. Vectorstore cache is evicted before the next request: `reset_vectorstore_cache()`
   at `src/rag/ingest.py:146`.

**NOT IMPLEMENTED:** A blue-green switch using a symlink or an atomic
directory rename (`os.rename`) that avoids the window between step 1 and
step 2 where the index is absent. During that window a concurrent `/ask`
request will get a cold cache miss and attempt to open a missing directory.
The vectorstore cache guards against this for already-cached stores, but
a first-request after cache eviction is not protected.

---

## 6. Rollback Procedure

**If a migration produces wrong results:**

1. **Revert** `EMBEDDING_MODEL` / `CHUNK_SIZE` / `CHUNK_OVERLAP` to the
   previous values in the deployment environment config.

2. **Re-trigger the index rebuild** — `POST /upload` with any corpus document
   for each affected tenant. This re-runs `build_tenant_index` with the reverted
   parameters.

3. **Confirm** — query a known document via `/ask`; check `index_version` in
   the response matches the reverted `VERSION_EMBEDDING`.

There is no checkpoint of the previous index. The raw corpus documents on disk
(`data/tenants/<slug>/corpus/`) are the source of truth; any compatible index
can always be reconstructed from them.

---

## 7. Corruption Recovery

If a Chroma SQLite file is corrupted (process killed mid-write, disk error):

1. **Delete** the affected tenant's index directory:
   ```
   rm -rf data/index/tenants/<slug>/chroma/
   ```
2. **Trigger a rebuild** via `POST /upload` with any document from the corpus,
   or directly call `build_tenant_index(tenant_id)`.
3. The `_release_chroma` helper (`src/rag/ingest.py:20–71`) clears chromadb's
   internal System cache (`SharedSystemClient.clear_system_cache()`) on every
   exit path, preventing the "Could not connect to default_tenant" error that
   occurs when a stopped chromadb System is reused.

---

## 8. Version Identifiers in Responses

Every `/ask` response includes version metadata in the `versions` field:

| Response field | Source constant | File |
|----------------|-----------------|------|
| `versions.model` | `VERSION_MODEL = CHAT_MODEL` | `src/rag/config.py:140` |
| `versions.prompt` | `VERSION_PROMPT = "v1"` | `src/rag/config.py:141` |
| `versions.embedding` | `VERSION_EMBEDDING = EMBEDDING_MODEL` | `src/rag/config.py:142` |
| `versions.retrieval` | `VERSION_RETRIEVAL = f"top_k={RETRIEVER_TOP_K}"` | `src/rag/config.py:143` |
| `versions.reranker` | `VERSION_RERANKER` | `src/rag/config.py:144` |
| `versions.eval_dataset` | `VERSION_EVAL_DATASET` | `src/rag/config.py:145` |

A change to `EMBEDDING_MODEL` is therefore observable in API responses without
requiring manual version tracking.

---

## 9. What Is NOT Automated

| Item | Status |
|------|--------|
| Bulk per-tenant index rebuild after a breaking config change | NOT IMPLEMENTED — must trigger `/upload` per tenant manually |
| Blue-green atomic switch (symlink-based) | NOT IMPLEMENTED — gap exists between rmtree and Chroma.from_documents |
| Automatic shadow eval before a breaking migration | NOT IMPLEMENTED — shadow eval is available (promotion_gate.py) but not wired |
| Index version pinned per-tenant in a registry | NOT IMPLEMENTED — `VERSION_EMBEDDING` is global, not per-tenant |
| Automatic rollback on quality regression after migration | NOT IMPLEMENTED — same constraint as model rollback (see MODEL_RELEASE_LIFECYCLE.md) |
