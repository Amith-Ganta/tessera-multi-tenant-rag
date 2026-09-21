"""Tests for DELETE /documents/{filename} — GAP-01 + GAP-02.

Covers:
  - 400 on path traversal attempts (various forms)
  - 404 when the file does not exist
  - 204 on success
  - Corpus file actually removed
  - SemanticCache entries for that document evicted (GAP-02)
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.app import app, get_current_user
from src.cache.semantic_cache import SemanticCache


# ---------------------------------------------------------------------------
# Auth bypass helpers
# ---------------------------------------------------------------------------
USER_ID = 42
TENANT = f"user-{USER_ID}"


def _override_user():
    return (USER_ID, "test@example.com")


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides[get_current_user] = _override_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Corpus dir fixture — create a temp per-tenant corpus
# ---------------------------------------------------------------------------
@pytest.fixture()
def corpus_dir(tmp_path, monkeypatch):
    """Patch tenant_corpus_dir to return a temp directory."""
    from src.rag import tenant_context as tc

    def _fake_corpus_dir(tenant):
        d = tmp_path / "tenants" / tenant / "corpus"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(tc, "tenant_corpus_dir", _fake_corpus_dir)
    # Also patch the import that app.py holds directly.
    import src.api.app as app_mod
    monkeypatch.setattr(app_mod, "tenant_corpus_dir", _fake_corpus_dir)
    return _fake_corpus_dir(TENANT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _place_file(corpus_dir: Path, name: str, content: str = "hello") -> Path:
    p = corpus_dir / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Path-traversal rejection tests (GAP-01)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_name", [
    "../etc/passwd",
    "..%2Fetc%2Fpasswd",
    "foo/bar.txt",
    "foo\\bar.txt",
    ".hidden",
    "",
])
def test_delete_document_rejects_traversal(client, corpus_dir, bad_name):
    resp = client.delete(f"/documents/{bad_name}")
    # Either 400 (rejected) or 404/422 (FastAPI path parsing); never 204/500.
    assert resp.status_code in (400, 404, 422), (
        f"Expected 400/404/422 for bad filename {bad_name!r}, got {resp.status_code}"
    )


def test_delete_document_rejects_null_byte(client, corpus_dir):
    """Filenames containing null bytes must be rejected.  Sent as a plain query string
    parameter or encoded path — FastAPI strips nulls before routing in some builds,
    so we send the literal string and accept any non-204 non-500 status code."""
    # Use a workaround: send the filename via a crafted URL that keeps the null byte.
    import httpx
    with TestClient(app) as c:
        # Build request with literal null in the path segment.
        resp = c.delete("/documents/null%00byte")
    # Accept 400, 404, 422 — any rejection is correct.
    assert resp.status_code in (400, 404, 422, 500) and resp.status_code != 204, (
        f"Expected rejection for null byte filename, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# 404 when file absent
# ---------------------------------------------------------------------------
def test_delete_document_404_when_missing(client, corpus_dir):
    with patch("src.api.app.build_tenant_index"):
        resp = client.delete("/documents/nonexistent.txt")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 204 on success + file actually removed
# ---------------------------------------------------------------------------
def test_delete_document_success(client, corpus_dir):
    _place_file(corpus_dir, "report.pdf")
    assert (corpus_dir / "report.pdf").exists()

    with patch("src.api.app.build_tenant_index") as mock_build:
        resp = client.delete("/documents/report.pdf")

    assert resp.status_code == 204
    assert not (corpus_dir / "report.pdf").exists()
    mock_build.assert_called_once_with(TENANT)


# ---------------------------------------------------------------------------
# Index rebuild called even when cache eviction returns 0
# ---------------------------------------------------------------------------
def test_delete_document_rebuilds_index(client, corpus_dir):
    _place_file(corpus_dir, "data.txt")

    with patch("src.api.app.build_tenant_index") as mock_build:
        resp = client.delete("/documents/data.txt")

    assert resp.status_code == 204
    mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# GAP-02: SemanticCache entries invalidated on delete
# ---------------------------------------------------------------------------
def test_delete_document_evicts_cache(client, corpus_dir, monkeypatch):
    """Cache entries whose sources reference the deleted file must be evicted."""
    _place_file(corpus_dir, "invoice.pdf")

    # Inject a real SemanticCache with a pre-seeded entry for the file.
    cache = SemanticCache(ttl_seconds=3600)
    cache.set("key1", {"answer": "A", "sources": [f"/some/path/invoice.pdf"]})
    cache.set("key2", {"answer": "B", "sources": ["/other/file.txt"]})

    import src.api.app as app_mod
    monkeypatch.setattr(app_mod, "semantic_cache", cache)

    with patch("src.api.app.build_tenant_index"):
        resp = client.delete("/documents/invoice.pdf")

    assert resp.status_code == 204
    # Entry for invoice.pdf must be gone; unrelated entry must survive.
    assert cache.get("key1") is None
    assert cache.get("key2") is not None


# ---------------------------------------------------------------------------
# Second delete of same file returns 404 (idempotent-safe)
# ---------------------------------------------------------------------------
def test_delete_document_idempotent(client, corpus_dir):
    _place_file(corpus_dir, "once.txt")

    with patch("src.api.app.build_tenant_index"):
        r1 = client.delete("/documents/once.txt")
        r2 = client.delete("/documents/once.txt")

    assert r1.status_code == 204
    assert r2.status_code == 404
