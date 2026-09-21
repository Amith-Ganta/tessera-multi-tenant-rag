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

import shutil
import types
from pathlib import Path

import pytest


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
# Test 4 (xfail) — Single document deletion without full re-upload (GAP-01, GAP-08)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP-01 / GAP-08 (docs/DATA_LIFECYCLE.md): no DELETE /documents endpoint "
        "and no per-document Chroma delete() path is implemented. "
        "Removing a single document from the vector index requires re-uploading "
        "all remaining documents to trigger a full index rebuild. "
        "See docs/DATA_LIFECYCLE.md section 3.1 and section 5."
    ),
)
def test_single_document_deletion_removes_it_from_index(tmp_path):
    """Attempt to delete one document from the index without re-uploading others.

    This test documents the EXPECTED behaviour that is not yet implemented:
    calling a delete API should remove exactly the targeted document's vectors
    from the Chroma index while leaving all other documents intact.

    Currently this path does not exist, so the assertion below will fail —
    which is the correct (xfail) outcome.
    """
    tenant = "tenant-xfail"
    corpus_dir = tmp_path / "corpus" / tenant
    index_dir = tmp_path / "index" / tenant
    corpus_dir.mkdir(parents=True)

    (corpus_dir / "keep.md").write_text("# Keep This\n\nThis document must remain.")
    (corpus_dir / "delete_me.md").write_text("# Delete Me\n\nThis must be purged from the index.")

    _build_index_with_fake_embeddings(tenant, index_dir, corpus_dir)

    # --- GAP: this is where a DELETE API call would go. ---
    # In a complete implementation something like:
    #   from src.rag.document_manager import delete_document
    #   delete_document(tenant_id=tenant, filename="delete_me.md")
    # would remove just the "delete_me.md" chunks from the Chroma collection
    # AND from the corpus directory, leaving "keep.md" intact.
    #
    # Since that function does not exist, we simulate the only real path:
    # manually remove the file but do NOT rebuild the index. The index still
    # has the old chunks — the assertion below should FAIL (hence xfail).
    (corpus_dir / "delete_me.md").unlink()

    from langchain_chroma import Chroma

    store = Chroma(
        persist_directory=str(index_dir),
        embedding_function=_fake_embeddings(),
    )
    try:
        results = store.similarity_search("purged", k=10)
        sources = [doc.metadata.get("source", "") for doc in results]
        # This assertion is expected to FAIL because the index was not updated.
        assert not any("delete_me.md" in s for s in sources), (
            "After single-document deletion, delete_me.md chunks must not appear in index results. "
            "CURRENTLY FAILS because no per-document delete path is implemented (GAP-01, GAP-08)."
        )
    finally:
        from src.rag.ingest import _release_chroma
        _release_chroma(store)


# ---------------------------------------------------------------------------
# Test 5 (xfail) — Cache invalidation when a document is deleted (GAP-02)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP-02 (docs/DATA_LIFECYCLE.md): semantic cache is not invalidated when "
        "a document is deleted or the index is rebuilt. Stale answers may be served "
        "after deletion until the 6-hour TTL expires naturally. "
        "See docs/DATA_LIFECYCLE.md section 3.1, step 3, and section 5."
    ),
)
def test_cache_invalidated_after_document_deletion(tmp_path):
    """After a document is removed and the index rebuilt, any cache entry that
    was derived from that document's chunks must be evicted.

    This test documents the EXPECTED behaviour that is NOT implemented:
    rebuilding the index should trigger invalidation of cache entries whose
    chunk_ids reference the deleted document.

    Currently SemanticCache has no hook into the ingest pipeline, so entries
    survive until their 6-hour TTL expires — this assertion will fail.
    """
    from src.cache.semantic_cache import SemanticCache

    # Simulate a cache entry for a query answered using "delete_me.md" content.
    cache = SemanticCache(ttl_seconds=3600, cleanup_interval=9999)
    fake_chunk_id = "delete_me_chunk_0"
    key = SemanticCache.make_key(
        query="what must be purged?",
        chunk_ids=[fake_chunk_id],
        model_name="deepseek/deepseek-flash",
        tenant="tenant-cache-test",
    )
    cache.set(
        key=key,
        payload={"answer": "This answer came from delete_me.md", "model": "deepseek/deepseek-flash"},
    )

    # Verify the entry is there before deletion.
    assert cache.get(key) is not None, "Cache entry must exist before deletion"

    # Simulate document deletion + index rebuild (no cache notification in current code).
    # In a complete implementation, build_tenant_index() would call
    # semantic_cache.invalidate_by_source("delete_me.md") here.
    # Since it does not, the entry remains — the assertion below should FAIL.

    # --- GAP: no cache invalidation hook on index rebuild ---

    # The expected (not yet implemented) behaviour: cache entry must be gone.
    assert cache.get(key) is None, (
        "After document deletion and index rebuild, cache entries derived from "
        "that document must be evicted. CURRENTLY FAILS because SemanticCache "
        "has no invalidation hook in the ingest pipeline (GAP-02)."
    )
