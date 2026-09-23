# Runbook: Chroma Index Rebuild

**Severity:** P1  
**Estimated resolution time:** < 30 min per tenant (depends on corpus size and embedding latency)  
**On-call trigger:** Empty `sources` in `/ask` response; ChromaDB exception in logs for a specific tenant

---

## 1. Detect

```bash
# Identify affected tenant from logs
grep -i "chroma\|vectorstore\|embed" logs/app.log | grep -i "error\|exception" | tail -20

# Confirm the index directory exists
ls -la data/index/tenants/<slug>/chroma/
```

If the directory is missing or empty, the index needs rebuilding.

---

## 2. Confirm raw corpus is intact

```bash
ls -la data/tenants/<slug>/corpus/
```

If corpus files are present, the index can be rebuilt without data loss.
If corpus files are also missing, restore from backup first — see `docs/DISASTER_RECOVERY.md §2.3`.

---

## 3. Rebuild (via API upload)

The fastest path: re-upload any one document from the corpus.
The upload endpoint wipes the existing index and rebuilds from all files in the corpus directory.

```bash
# Authenticate as the affected tenant
TOKEN=$(curl -sf -X POST http://127.0.0.1:8000/token \
  -d "username=<username>&password=<password>" | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Re-upload any corpus file to trigger a full index rebuild
curl -sf -X POST http://127.0.0.1:8000/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@data/tenants/<slug>/corpus/<any_file>"
```

---

## 4. Rebuild (programmatic)

```python
from src.rag.ingest import build_tenant_index
build_tenant_index("<slug>")
```

---

## 5. Verify

```bash
curl -sf -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the main topics in the uploaded document?"}' \
  | python -m json.tool
# Expect: non-empty "sources" array
```

---

## 6. Notes

- A full rebuild re-embeds every chunk in the corpus. At ~$0.000 001 per query embedding
  (text-embedding-3-small) the cost is negligible for corpora under 10 MB.
- The rebuild is tenant-scoped. Other tenants are unaffected throughout.
- Per-document Chroma deletion is not implemented (GAP-08, deferred). A full rebuild
  is always required after any document change.
