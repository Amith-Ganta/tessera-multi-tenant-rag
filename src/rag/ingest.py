"""Build and persist the dense index from the local markdown corpus."""

from __future__ import annotations

import gc
import shutil
from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.document_loaders import TextLoader
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import CHUNK_OVERLAP, CHUNK_SIZE, CORPUS_DIR, EMBEDDING_MODEL, INDEX_DIR, get_openai_api_key
from .retriever_dense import reset_vectorstore_cache
from .retriever_sparse import invalidate_bm25_cache
from .tenant_context import tenant_corpus_dir, tenant_index_dir


def _release_chroma(store: "Chroma | None") -> None:
    """Tear a Chroma store down fully so the next build on the same path is clean.

    Two things have to happen, in order, or repeated uploads break on Windows:

    1. ``system.stop()`` shuts the embedded server and releases the SQLite file
       handle. Without it the handle leaks: the next rebuild cannot fully wipe
       the directory, so re-embedding appends to the old collection and the
       index accumulates duplicate vectors.

    2. ``SharedSystemClient.clear_system_cache()`` evicts chromadb's per-path
       client/system singleton. chromadb caches a System per persist_directory;
       ``stop()`` kills that cached System's Rust bindings but does NOT remove
       the dead entry from the cache. The next ``PersistentClient`` on the same
       path then reuses the stopped System and dies with
       "Could not connect to tenant default_tenant". Clearing the cache forces a
       fresh System on the next build. This is exactly the destruction path a
       re-upload and a provider fallback hit, so both steps must run on every
       exit from the build.

    Verified on chromadb 1.5.9: stop-only fails the next same-path rebuild;
    clear-only rebuilds but duplicates vectors; stop + clear rebuilds cleanly
    with no duplicates.
    """
    if store is not None:
        try:
            client = getattr(store, "_client", None)
            if client is not None:
                system = getattr(client, "_system", None)
                if system is not None and hasattr(system, "stop"):
                    try:
                        system.stop()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                del store
            except Exception:
                pass
            gc.collect()

    # Evict chromadb's cached System for this (and every) path. Safe to call even
    # when store is None, and must run after stop() so the next build rebuilds
    # the System from scratch instead of reusing a stopped one.
    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass


def load_documents() -> list:
    docs = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        docs.extend(TextLoader(str(path), encoding="utf-8").load())
    return docs


def build_index() -> Chroma:
    # Use OpenAI embeddings for a strong baseline and to mirror Project 1's setup.
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    documents = splitter.split_documents(load_documents())
    get_openai_api_key()
    embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    return Chroma.from_documents(documents=documents, embedding=embeddings, persist_directory=str(INDEX_DIR))


def build_tenant_index(
    tenant_id: str,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> dict[str, object]:
    corpus_dir = tenant_corpus_dir(tenant_id)
    index_dir = tenant_index_dir(tenant_id)
    corpus_dir.mkdir(parents=True, exist_ok=True)

    docs = []
    # Accept both markdown and plain text so uploads of either type are indexed.
    for path in sorted(corpus_dir.glob("*.md")) + sorted(corpus_dir.glob("*.txt")):
        docs.extend(TextLoader(str(path), encoding="utf-8").load())

    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_documents(docs)
    get_openai_api_key()
    embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL)

    # Rebuild from scratch every time. Re-embedding into a persist_directory that
    # already holds a collection appends duplicate vectors, so each re-upload would
    # skew retrieval and grow the index without bound. Wiping first makes the build
    # idempotent: the index always reflects exactly the current corpus on disk.
    if index_dir.exists():
        shutil.rmtree(index_dir, ignore_errors=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    store = None
    try:
        store = Chroma.from_documents(
            documents=chunks,
            embedding=embeddings,
            persist_directory=str(index_dir),
        )
        result = {
            "tenant_id": tenant_id,
            "docs": len(docs),
            "chunks": len(chunks),
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "index_dir": str(index_dir),
        }
    finally:
        # Always release the SQLite handle, even if embedding raised partway. On
        # Windows a leaked handle blocks the next upload/query with a file lock,
        # and it is exactly the destruction path a provider fallback hits under
        # load, so teardown must run on every exit from this function.
        _release_chroma(store)

    # Evict the BM25 LRU cache so the next sparse retrieval rebuilds from the
    # updated corpus on disk rather than serving stale pre-upload documents.
    invalidate_bm25_cache(str(corpus_dir))

    # Evict the dense vectorstore cache so the next retrieval opens a fresh
    # Chroma client against the newly rebuilt index rather than the pre-upload one.
    reset_vectorstore_cache()

    return result


def main() -> None:
    build_index()
    print(f"Built Chroma index at {INDEX_DIR}")


if __name__ == "__main__":
    main()
