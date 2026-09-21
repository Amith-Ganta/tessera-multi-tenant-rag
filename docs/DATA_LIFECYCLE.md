# Data Lifecycle and Deletion Propagation

**Tessera Multi-Tenant RAG API**  
Last updated: 2026-09-21

---

## 1. Data Classification Table

| Data Type | System of Record | Derived From | Retention Policy | Deletion Semantics | Tenant Isolation |
|---|---|---|---|---|---|
| Raw document | Disk — `data/tenants/<slug>/corpus/` | User upload | Until re-upload wipes index; file persists separately | **NOT IMPLEMENTED** — no DELETE endpoint | Per-tenant directory |
| Chunks | In-memory only (transient) | Raw document (splitting) | Discarded after ingestion; not persisted independently | N/A — never stored | N/A |
| Embeddings | Chroma persist dir (SQLite) | Chunks + OpenAI embedding model | Until index directory is wiped | Wiped by `shutil.rmtree` on next upload | Per-tenant Chroma dir |
| Vector index (Chroma) | Disk — `data/index/tenants/<slug>/chroma/` | Embeddings + chunk text + metadata | Rebuilt from scratch on every upload | `shutil.rmtree` before rebuild (`src/rag/ingest.py:112`) | Separate directory per tenant |
| Semantic cache entries | In-process memory (`SemanticCache._store`) | LLM answer + judge result | TTL 21 600 s (6 h); background eviction every 5 min | `SemanticCache.clear()` — **NOT EXPOSED VIA API** | Key prefix includes tenant slug |
| Judge results (in-process) | `JudgeStore` ring buffer (OrderedDict) | Eval run output | Ring buffer max 5 000 entries; LRU eviction | No explicit deletion | `trace_id` scoped; no tenant field in key |
| Judge results (Redis) | Redis — key `judge:result:<trace_id>` | Eval run output | TTL 86 400 s (24 h) | TTL expiry only; **no explicit delete API** | `trace_id` scoped; no tenant field in key |
| Judge queue jobs | Redis list — `judge:queue` | `/ask` request | Consumed on dequeue; no persistence after processing | Consumed by worker; DLQ entries persist indefinitely | No tenant isolation in queue |
| Judge DLQ | Redis list — `judge:queue:dlq` | Failed queue jobs | **No TTL — persists indefinitely** | **NOT IMPLEMENTED** — no drain endpoint | No tenant isolation |
| Conversation checkpoints (SQLite) | `data/checkpoints.sqlite3` — `checkpoints` table | A2A workflow state | Until explicit delete after workflow completion | `SQLiteCheckpointer.delete_state(thread_id)` — `src/rag/checkpointer.py:112` | `thread_id` scoped; tenant stored in JSON state |
| Conversation checkpoints (Redis) | Redis — key `checkpoint:<thread_id>` | A2A workflow state | TTL 86 400 s (24 h) | `RedisCheckpointer.delete_state(thread_id)` — `src/state/redis_checkpointer.py:83` | `thread_id` scoped; tenant stored in JSON state |
| Analytics records | `logs/analytics.jsonl` (JSONL append-only) | Every `/ask` call | **No TTL — file grows unbounded** | `clear_analytics()` — **NOT EXPOSED VIA API** | `tenant` field present but file is shared |
| User query log | `tessera_users.db` — `user_queries` table | Every `/ask` call | **No TTL — grows unbounded** | **NOT IMPLEMENTED** — no delete endpoint | `user_id` field present; shared database |
| Langfuse / LangSmith traces | Remote (Langfuse / LangSmith cloud) | LLM calls and RAG runs | Governed by external service SLA | Via external provider UI only; **not implemented locally** | Remote project scoping |
| Request logs | stdout / stderr only | Every request | Ephemeral — lives in container log retention only | N/A | Structured log fields include `tenant` |

---

## 2. Data Pipeline

```
User upload (/upload)
    │
    ▼
Raw document written to disk
    data/tenants/<slug>/corpus/<filename>
    src/api/app.py:311-315
    │
    ▼
Chroma index directory wiped (if exists)
    shutil.rmtree(data/index/tenants/<slug>/chroma/)
    src/rag/ingest.py:112-114
    │
    ▼
Chunking (in-memory only)
    RecursiveCharacterTextSplitter(CHUNK_SIZE=800, CHUNK_OVERLAP=120)
    src/rag/ingest.py:81-86
    │
    ▼
Embedding (OpenAI text-embedding-3-small)
    Called inside Chroma.from_documents()
    src/rag/ingest.py:118-122
    │
    ▼
Chroma index written to disk
    data/index/tenants/<slug>/chroma/
    src/rag/ingest.py:120-122
    │
    ▼── /ask request ──────────────────────────────────────────────────────┐
    │                                                                       │
    ▼                                                                       │
Retrieval (dense / sparse / hybrid)                                         │
    Dense: embed query → similarity_search_by_vector_with_relevance_scores  │
    Sparse: BM25 via retrieve_sparse()                                      │
    Hybrid: fused score (RRF), optional cross-encoder rerank               │
    │                                                                       │
    ▼                                                                       │
Semantic cache lookup                                                       │
    Key: sha256(tenant|query|chunk_ids|model)                              │
    Hit → return cached answer (no LLM call)                               │
    Miss → continue                                                         │
    │                                                                       │
    ▼                                                                       │
LLM answer generation                                                       │
    Via LiteLLM + _FALLBACK_CHAIN (circuit breaker + bulkhead)             │
    src/rag/llm.py                                                          │
    │                                                                       │
    ▼                                                                       │
Analytics written                                                           │
    logs/analytics.jsonl (append)    src/rag/analytics.py                  │
    tessera_users.db:user_queries    src/auth/auth.py                      │
    │                                                                       │
    ▼                                                                       │
Judge job submitted (async / queue)                                         │
    judge:queue (Redis LPUSH) or asyncio.create_task                        │
    src/judge/async_runner.py                                               │
    │                                                                       │
    ▼                                                                       │
Judge evaluation runs                                                       │
    Result written to judge:result:<trace_id> (Redis, TTL 24 h)           │
    or JudgeStore ring buffer (in-process)                                  │
    │                                                                       │
    ▼                                                                       │
Semantic cache populated (after successful judge)                           │
    src/judge/async_runner.py:58-65                                        │
    └───────────────────────────────────────────────────────────────────────┘

A2A workflow path (/ask with strategy=a2a):
    Checkpoint saved at start: data/checkpoints.sqlite3 or Redis
    Checkpoint updated on each step
    Checkpoint deleted after workflow completes
        src/orchestrator/a2a_supervisor.py:376
```

---

## 3. Deletion Propagation

### 3.1 What Must Be Deleted When a Tenant Deletes a Document

When a tenant removes a document, the following artifacts must be cleaned up to
achieve full deletion consistency:

| Step | Artifact | Location | Implemented? |
|---|---|---|---|
| 1 | Raw document file | `data/tenants/<slug>/corpus/<filename>` | **NOT IMPLEMENTED** |
| 2 | Vector index (embeddings + chunks for that document) | `data/index/tenants/<slug>/chroma/` | **PARTIAL** — full index wipe only (re-upload), no per-document removal |
| 3 | Semantic cache entries that used this document's chunks | `SemanticCache._store` (in-memory) | **NOT IMPLEMENTED** — no per-document eviction |
| 4 | Judge results that reference this document's chunks | Redis `judge:result:*` / `JudgeStore` | **NOT IMPLEMENTED** |
| 5 | Analytics records mentioning this document | `logs/analytics.jsonl`, `user_queries` table | **NOT IMPLEMENTED** |

**Current actual deletion path (on re-upload only):**

```
POST /upload (new document for same tenant)
    │
    ├── Wipes entire Chroma index directory (shutil.rmtree)
    │   src/rag/ingest.py:112-114
    │
    └── Does NOT touch:
        - Old raw document file (new file is written alongside it)
        - Semantic cache entries
        - Judge results
        - Analytics records
```

There is **no dedicated DELETE /documents endpoint** and **no DELETE /tenant endpoint**.

### 3.2 What Must Be Deleted When a Tenant Is Deleted

| Artifact | Location | Implemented? |
|---|---|---|
| All raw documents | `data/tenants/<slug>/corpus/` | **NOT IMPLEMENTED** |
| Vector index | `data/index/tenants/<slug>/chroma/` | **NOT IMPLEMENTED** |
| Semantic cache entries (all for tenant) | `SemanticCache._store` | **NOT IMPLEMENTED** |
| Judge results (all for tenant) | Redis `judge:result:*` / `JudgeStore` | **NOT IMPLEMENTED** — no tenant field in key |
| Conversation checkpoints (all for tenant) | `data/checkpoints.sqlite3` / Redis `checkpoint:*` | **NOT IMPLEMENTED** — tenant is in JSON state body, not the key; no tenant-level query |
| Analytics records | `logs/analytics.jsonl`, `user_queries` table | **NOT IMPLEMENTED** |
| User account | `tessera_users.db:users` | **NOT IMPLEMENTED** |

### 3.3 What IS Implemented (Partial Paths)

| Operation | Trigger | Files/Code |
|---|---|---|
| Wipe entire per-tenant Chroma index | Re-upload of any document | `src/rag/ingest.py:112-114` |
| Delete single conversation checkpoint | A2A workflow completes | `src/orchestrator/a2a_supervisor.py:376` |
| Redis checkpoint TTL expiry | 24 h after last write | `src/state/redis_checkpointer.py:63` |
| Judge result TTL expiry (Redis) | 24 h after result written | `src/judge/redis_queue.py:118` |
| Semantic cache TTL eviction | 6 h after entry written; background sweep every 5 min | `src/cache/semantic_cache.py:89-103` |

---

## 4. Backup and Restore

### 4.1 What Needs Backing Up

| Data | Location | Backup Approach |
|---|---|---|
| Raw documents | `data/tenants/*/corpus/` | File system snapshot or `tar -czf` |
| Chroma vector indices | `data/index/tenants/*/chroma/` | File system snapshot — Chroma's SQLite files are portable |
| SQLite checkpoint DB | `data/checkpoints.sqlite3` | Copy file when no active A2A workflows |
| User database | `tessera_users.db` | Copy file |
| Analytics log | `logs/analytics.jsonl` | Copy file |

### 4.2 What Does NOT Need Backing Up

| Data | Reason |
|---|---|
| Semantic cache | In-memory only; rebuilt from queries on cache miss |
| Judge queue / DLQ | At-most-once queue; in-flight jobs on crash are not retried |
| Judge results (Redis) | 24 h TTL; used for polling only; derived from re-run if needed |
| Conversation checkpoints (Redis) | 24 h TTL; SQLite is the durable backend |
| LangSmith / Langfuse traces | Stored in external cloud; not owned by Tessera |

### 4.3 Restore Procedure

**Full restore from backup:**

1. Stop the API process.
2. Restore `data/` directory tree (documents + Chroma indices + checkpoint DB).
3. Restore `tessera_users.db`.
4. Restore `logs/analytics.jsonl` (optional; non-functional).
5. Restart the API process. The vectorstore cache (`_VECTORSTORE_CACHE`) is cold on
   start and will reload from the Chroma persist directories on first request.

**Partial document restore (single tenant):**

1. Restore `data/tenants/<slug>/corpus/` files.
2. Call `POST /upload` for each document to rebuild the Chroma index.
   (The index cannot be directly restored from a Chroma backup if the embedding model
   or chunk settings have changed — rebuilding from source is always safe.)

---

## 5. Known Gaps

The following data lifecycle capabilities are **not implemented**. Each gap is a
future enhancement; none affects current functionality.

| Gap ID | Description | Impact |
|---|---|---|
| GAP-01 | No `DELETE /documents/{filename}` endpoint | Cannot remove a single document without re-uploading all others |
| GAP-02 | Document deletion does not invalidate semantic cache | Stale answers may be served after a document is removed |
| GAP-03 | Document deletion does not remove judge results | Orphaned judge results for deleted content remain in Redis until TTL expiry |
| GAP-04 | No `DELETE /tenant/{slug}` endpoint | Full tenant data removal requires manual filesystem and database operations |
| GAP-05 | Analytics log and `user_queries` table grow unbounded | No retention policy, rotation, or pruning endpoint |
| GAP-06 | Judge DLQ has no TTL and no drain API endpoint | Failed jobs accumulate indefinitely; requires direct Redis access to clear |
| GAP-07 | Checkpoint deletion is not triggered by tenant deletion | Orphaned checkpoints remain until TTL expiry (Redis) or manual DB query (SQLite) |
| GAP-08 | No per-document Chroma deletion | Chroma's `delete()` API exists but is not wired up; only full-index wipe is implemented |
| GAP-09 | Judge results have no tenant field in Redis key | Cannot enumerate or purge all judge results for a given tenant without a scan |
| GAP-10 | `clear_analytics()` and `SemanticCache.clear()` are not exposed via API | No operational path to flush these without code changes |
