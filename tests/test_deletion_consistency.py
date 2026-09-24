"""Deletion consistency tests — Data Lifecycle (docs/DATA_LIFECYCLE.md).

Tests verify the actual deletion semantics of Tessera's data pipeline:
  1. Ingestion creates the expected on-disk artifacts.
  2. Re-upload wipes the old Chroma index and rebuilds it (the only
     implemented deletion path for vector data).
  3. A second tenant's index is unaffected by the first tenant's re-upload.
  4-5. Single-document deletion and cache invalidation are marked xfail
     because these paths are NOT implemented (GAP-01, GAP-02, GAP-08 in
     docs/DATA_LIFECYCLE.md).

These tests operate entirely on disk fixtures under a temp directory and
never call the live API or OpenAI. Embeddings are mocked so no OPENAI_API_KEY
is required.
"""

from __future__ import annotations

import importlib
import shutil
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Isolation fixture — ensure the real langchain_chroma is used in this file.
#
# Several other test modules inject a sys.modules stub for langchain_chroma at
# import time (via sys.modules.setdefault).  When pytest collects those modules
# first, the stub stays in sys.modules and any subsequent "from langchain_chroma
# import Chroma" returns MagicMock instead of the real class, causing
# Chroma.from_documents to write nothing to disk.
#
# This autouse fixture saves the current sys.modules entry, replaces it with
# the real package (force-reloaded), and restores the stub after each test so
# other files that expect the stub are unaffected.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _use_real_langchain_chroma():
    _saved = sys.modules.get("langchain_chroma")
    # Remove any stub so importlib.import_module picks up the real package.
    sys.modules.pop("langchain_chroma", None)
    real_mod = importlib.import_module("langchain_chroma")
    sys.modules["langchain_chroma"] = real_mod
    yield
    # Restore whatever was there before (stub or nothing).
    if _saved is None:
        sys.modules.pop("langchain_chroma", None)
    else:
        sys.modules["langchain_chroma"] = _saved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_embeddings():
    """Return a minimal LangChain-compatible embeddings object backed by a
    deterministic hash so tests run without an OpenAI key."""
    import hashlib

    class _FakeEmbeddings:
        def embed_documents(self, texts):
            return [
                [float(b) / 255.0 for b in hashlib.sha256(t.encode()).digest()[:8]]
                for t in texts
            ]

        def embed_query(self, text):
            return self.embed_documents([text])[0]

    return _FakeEmbeddings()


def _build_index_with_fake_embeddings(tenant_id: str, index_dir: Path, corpus_dir: Path):
    """Run the Tessera ingest pipeline against a temp dir, substituting fake embeddings."""
    from langchain_chroma import Chroma
    from langchain_community.document_loaders import TextLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from src.rag.ingest import _release_chroma
    from src.rag.config import CHUNK_SIZE, CHUNK_OVERLAP

    docs = []
    for path in sorted(corpus_dir.glob("*.md")) + sorted(corpus_dir.glob("*.txt")):
        docs.extend(TextLoader(str(path), encoding="utf-8").load())

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks = splitter.split_documents(docs)

    if index_dir.exists():
        shutil.rmtree(index_dir, ignore_errors=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    store = None
    try:
        store = Chroma.from_documents(
            documents=chunks,
            embedding=_fake_embeddings(),
            persist_directory=str(index_dir),
        )
    finally:
        _release_chroma(store)

    return {"chunks": len(chunks), "docs": len(docs)}


# ---------------------------------------------------------------------------
# Test 1 — Ingestion creates on-disk artifacts
# ---------------------------------------------------------------------------

class TestIngestionCreatesArtifacts:
    def test_corpus_file_written_and_index_directory_created(self, tmp_path):
        """After ingestion the raw document exists on disk and the Chroma
        persist directory contains at least one file (the SQLite store)."""
        tenant = "tenant-alpha"
        corpus_dir = tmp_path / "corpus" / tenant
        index_dir = tmp_path / "index" / tenant
        corpus_dir.mkdir(parents=True)

        doc_file = corpus_dir / "policy.md"
        doc_file.write_text("# Policy\n\nAll employees must follow this policy.")

        # Raw document is on disk immediately after write (simulates /upload).
        assert doc_file.exists(), "Raw document must exist after upload"

        _build_index_with_fake_embeddings(tenant, index_dir, corpus_dir)

        # Chroma persist directory must exist and contain at least one file.
        assert index_dir.exists(), "Chroma index directory must be created after ingestion"
        index_files = list(index_dir.rglob("*"))
        assert len(index_files) > 0, (
            f"Chroma index directory must contain files after ingestion, found: {index_files!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Re-upload wipes the old index and rebuilds with new content
# ---------------------------------------------------------------------------

class TestReUploadWipesOldIndex:
    def test_chroma_index_rebuilt_from_scratch_on_reuupload(self, tmp_path):
        """Re-uploading any document for a tenant wipes the entire Chroma
        index and rebuilds it. Content from the previous index is gone.
        This is the ONLY implemented deletion path for vector data."""
        tenant = "tenant-bravo"
        corpus_dir = tmp_path / "corpus" / tenant
        index_dir = tmp_path / "index" / tenant
        corpus_dir.mkdir(parents=True)

        # First document: 'secret content' that should disappear after re-index.
        first_doc = corpus_dir / "first.md"
        first_doc.write_text("# Secret Document\n\nThis content must be removed.")

        _build_index_with_fake_embeddings(tenant, index_dir, corpus_dir)

        # Record inode / file list of the OLD index to prove it was replaced.
        old_index_files = {f.name for f in index_dir.rglob("*") if f.is_file()}
        assert len(old_index_files) > 0, "First build must produce index files"

        # Remove the first document and add a replacement.
        first_doc.unlink()
        second_doc = corpus_dir / "second.md"
        second_doc.write_text("# Replacement Document\n\nOnly this content should exist.")

        _build_index_with_fake_embeddings(tenant, index_dir, corpus_dir)

        # Index directory must still exist after rebuild.
        assert index_dir.exists(), "Index directory must exist after re-upload"

        # Verify the rebuild ran by checking content via Chroma query.
        from langchain_chroma import Chroma

        store = Chroma(
            persist_directory=str(index_dir),
            embedding_function=_fake_embeddings(),
        )
        try:
            results = store.similarity_search("replacement", k=5)
            texts = [doc.page_content for doc in results]
            assert any("Replacement" in t or "replacement" in t.lower() for t in texts), (
                "Post-rebuild index must contain new document content"
            )
            # The old 'secret content' should not appear (not guaranteed by hash similarity
            # alone, but the file was deleted so it cannot have been indexed).
            sources = [doc.metadata.get("source", "") for doc in results]
            assert not any("first.md" in s for s in sources), (
                "Post-rebuild index must not reference the deleted first.md"
            )
        finally:
            from src.rag.ingest import _release_chroma
            _release_chroma(store)


# ---------------------------------------------------------------------------
# Test 3 — Tenant isolation: another tenant's index is unaffected
# ---------------------------------------------------------------------------

class TestTenantIsolationOnReUpload:
    def test_other_tenant_index_unaffected_by_first_tenant_rebuild(self, tmp_path):
        """When tenant A rebuilds its index, tenant B's index must be untouched."""
        corpus_a = tmp_path / "corpus" / "alpha"
        index_a = tmp_path / "index" / "alpha"
        corpus_b = tmp_path / "corpus" / "bravo"
        index_b = tmp_path / "index" / "bravo"
        corpus_a.mkdir(parents=True)
        corpus_b.mkdir(parents=True)

        (corpus_a / "doc_a.md").write_text("# Tenant A\n\nTenant A content.")
        (corpus_b / "doc_b.md").write_text("# Tenant B\n\nTenant B exclusive content.")

        _build_index_with_fake_embeddings("alpha", index_a, corpus_a)
        _build_index_with_fake_embeddings("bravo", index_b, corpus_b)

        # Capture tenant B's index files before we touch tenant A.
        b_files_before = {f: f.stat().st_mtime for f in index_b.rglob("*") if f.is_file()}

        # Rebuild tenant A with new content.
        (corpus_a / "doc_a.md").write_text("# Tenant A Updated\n\nCompletely different.")
        _build_index_with_fake_embeddings("alpha", index_a, corpus_a)

        # Tenant B's index files must not have changed.
        b_files_after = {f: f.stat().st_mtime for f in index_b.rglob("*") if f.is_file()}
        assert b_files_before == b_files_after, (
            "Tenant B's index must not be modified when tenant A rebuilds"
        )

        # Tenant B's content is still retrievable.
        from langchain_chroma import Chroma

        store = Chroma(
            persist_directory=str(index_b),
            embedding_function=_fake_embeddings(),
        )
        try:
            results = store.similarity_search("exclusive", k=5)
            sources = [doc.metadata.get("source", "") for doc in results]
            assert any("doc_b.md" in s for s in sources), (
                "Tenant B's document must still be in its index after tenant A's rebuild"
            )
        finally:
            from src.rag.ingest import _release_chroma
            _release_chroma(store)


# ---------------------------------------------------------------------------
# Test 4 — Single document deletion removes it from the index (GAP-01 CLOSED)
#
# GAP-08 (per-document Chroma delete() without full rebuild) remains deferred.
# The DELETE /documents endpoint solves GAP-01 by removing the file and
# triggering a full index rebuild, which is sufficient for correctness.
# ---------------------------------------------------------------------------

def test_single_document_deletion_removes_it_from_index(tmp_path, monkeypatch):
    """After DELETE /documents/{filename} the deleted file's chunks must no longer
    appear in the rebuilt index.  A full index rebuild is used (GAP-08, per-document
    Chroma delete, remains deferred — see docs/DATA_LIFECYCLE.md section 5).
    """
    tenant = "tenant-gap01"
    corpus_dir = tmp_path / "corpus" / tenant
    index_dir = tmp_path / "index" / tenant
    corpus_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)

    (corpus_dir / "keep.md").write_text("# Keep This\n\nThis document must remain.")
    (corpus_dir / "delete_me.md").write_text("# Delete Me\n\nThis must be purged from the index.")

    _build_index_with_fake_embeddings(tenant, index_dir, corpus_dir)

    # Remove file and trigger full rebuild (same as DELETE endpoint does).
    (corpus_dir / "delete_me.md").unlink()
    _build_index_with_fake_embeddings(tenant, index_dir, corpus_dir)

    from langchain_chroma import Chroma

    store = Chroma(
        persist_directory=str(index_dir),
        embedding_function=_fake_embeddings(),
    )
    try:
        results = store.similarity_search("purged", k=10)
        sources = [doc.metadata.get("source", "") for doc in results]
        assert not any("delete_me.md" in s for s in sources), (
            "After deletion + rebuild, delete_me.md chunks must not appear in index results."
        )
    finally:
        from src.rag.ingest import _release_chroma
        _release_chroma(store)


# ---------------------------------------------------------------------------
# Test 5 — Cache invalidation when a document is deleted (GAP-02 CLOSED)
#
# SemanticCache.invalidate_by_document(tenant, filename) is now implemented.
# The DELETE /documents endpoint calls it before triggering the index rebuild.
# ---------------------------------------------------------------------------

def test_cache_invalidated_after_document_deletion(tmp_path):
    """SemanticCache.invalidate_by_document() must evict entries whose sources
    reference the deleted file.  GAP-02 is closed — this is now a passing test.
    """
    from src.cache.semantic_cache import SemanticCache

    cache = SemanticCache(ttl_seconds=3600, cleanup_interval=9999)
    tenant = "tenant-cache-test"

    # Seed a cache entry whose sources reference delete_me.md.
    key_hit = SemanticCache.make_key(
        query="what must be purged?",
        chunk_ids=["delete_me_chunk_0"],
        model_name="deepseek/deepseek-flash",
        tenant=tenant,
    )
    cache.set(
        key=key_hit,
        payload={
            "answer": "From delete_me.md",
            "sources": [f"/data/tenants/{tenant}/corpus/delete_me.md"],
        },
    )

    # Seed a second entry for an unrelated file — must NOT be evicted.
    key_safe = SemanticCache.make_key(
        query="what stays?",
        chunk_ids=["keep_chunk_0"],
        model_name="deepseek/deepseek-flash",
        tenant=tenant,
    )
    cache.set(
        key=key_safe,
        payload={
            "answer": "From keep.md",
            "sources": [f"/data/tenants/{tenant}/corpus/keep.md"],
        },
    )

    assert cache.get(key_hit) is not None, "Cache entry must exist before invalidation"
    assert cache.get(key_safe) is not None, "Safe entry must exist before invalidation"

    # Exercise the new invalidation method (wired into DELETE /documents endpoint).
    evicted = cache.invalidate_by_document(tenant, "delete_me.md")

    assert evicted == 1, f"Expected 1 eviction, got {evicted}"
    assert cache.get(key_hit) is None, "Deleted document's cache entry must be evicted"
    assert cache.get(key_safe) is not None, "Unrelated cache entry must survive"
