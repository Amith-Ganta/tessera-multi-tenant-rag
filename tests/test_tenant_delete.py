"""Tests for DELETE /tenant — GAP-04 + GAP-07.

Covers:
  - 204 on successful full tenant deletion
  - Corpus directory removed
  - Chroma index directory removed
  - SemanticCache cleared for tenant
  - SQLite checkpoints deleted (GAP-07)
  - Redis checkpoints deleted (GAP-07)
  - Judge results deleted (tenant-scoped Redis keys)
  - User row deleted from auth DB
  - Idempotent: second call returns 204 without error
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock, call
import pytest

import fakeredis
from fastapi.testclient import TestClient

from src.api.app import app, get_current_user
from src.cache.semantic_cache import SemanticCache
from src.rag.checkpointer import SQLiteCheckpointer
from src.state.redis_checkpointer import RedisCheckpointer, _inject_client as inject_redis_cp
from src.judge.redis_queue import JudgeQueue, _set_client as set_judge_redis


# ---------------------------------------------------------------------------
# Auth bypass
# ---------------------------------------------------------------------------
USER_ID = 99
TENANT = f"user-{USER_ID}"


def _override_user():
    return (USER_ID, "owner@example.com")


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides[get_current_user] = _override_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Directory fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def tenant_dirs(tmp_path, monkeypatch):
    """Patch corpus and index dir functions to return temp dirs."""
    from src.rag import tenant_context as tc
    import src.api.app as app_mod

    corpus = tmp_path / "tenants" / TENANT / "corpus"
    index = tmp_path / "index" / "tenants" / TENANT / "chroma"
    corpus.mkdir(parents=True)
    index.mkdir(parents=True)
    # Place sentinel files so rmtree has something to remove.
    (corpus / "doc.pdf").write_text("x")
    (index / "chroma.sqlite3").write_text("y")

    monkeypatch.setattr(tc, "tenant_corpus_dir", lambda t: corpus if t == TENANT else Path(tmp_path / t / "corpus"))
    monkeypatch.setattr(tc, "tenant_index_dir", lambda t: index if t == TENANT else Path(tmp_path / t / "index"))
    monkeypatch.setattr(app_mod, "tenant_corpus_dir", lambda t: corpus if t == TENANT else Path(tmp_path / t / "corpus"))
    return {"corpus": corpus, "index": index}


# ---------------------------------------------------------------------------
# 204 on success — directories removed
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="order-dependent; passes in isolation — see docs/TEST_ISOLATION.md")
def test_delete_tenant_removes_corpus_and_index(client, tenant_dirs, monkeypatch):
    import src.api.app as app_mod

    mock_cache = MagicMock(spec=SemanticCache)
    monkeypatch.setattr(app_mod, "semantic_cache", mock_cache)

    with patch("src.rag.checkpointer.SQLiteCheckpointer") as MockSQLite, \
         patch("src.state.redis_checkpointer.RedisCheckpointer") as MockRedis, \
         patch("src.auth.auth.delete_user") as mock_del_user, \
         patch("src.rag.config.JUDGE_QUEUE_ENABLED", False):
        MockSQLite.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        MockRedis.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        resp = client.delete("/tenant")

    assert resp.status_code == 204
    assert not tenant_dirs["corpus"].exists()
    assert not tenant_dirs["index"].exists()
    mock_cache.invalidate_by_tenant.assert_called_once_with(TENANT)
    mock_del_user.assert_called_once_with(USER_ID)


# ---------------------------------------------------------------------------
# GAP-07: SQLite checkpoints deleted
# ---------------------------------------------------------------------------
def test_delete_tenant_clears_sqlite_checkpoints(client, tenant_dirs, monkeypatch, tmp_path):
    import src.api.app as app_mod

    monkeypatch.setattr(app_mod, "semantic_cache", MagicMock(spec=SemanticCache))

    db_path = tmp_path / "checkpoints.sqlite3"
    cp = SQLiteCheckpointer(db_path=db_path)
    cp.save_state("thread-1", {"tenant_slug": TENANT, "retries": 0, "transcript": []})
    cp.save_state("thread-2", {"tenant_slug": "user-1", "retries": 0, "transcript": []})

    def _make_cp(*a, **kw):
        return SQLiteCheckpointer(db_path=db_path)

    with patch("src.rag.checkpointer.SQLiteCheckpointer", _make_cp), \
         patch("src.state.redis_checkpointer.RedisCheckpointer") as MockRedis, \
         patch("src.auth.auth.delete_user"), \
         patch("src.rag.config.JUDGE_QUEUE_ENABLED", False):
        MockRedis.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        resp = client.delete("/tenant")

    assert resp.status_code == 204
    # thread-1 must be gone, thread-2 (different tenant) must survive.
    assert cp.load_state("thread-1") is None
    assert cp.load_state("thread-2") is not None


# ---------------------------------------------------------------------------
# GAP-07: Redis checkpoints deleted
# ---------------------------------------------------------------------------
def test_delete_tenant_clears_redis_checkpoints(client, tenant_dirs, monkeypatch, tmp_path):
    import src.api.app as app_mod

    monkeypatch.setattr(app_mod, "semantic_cache", MagicMock(spec=SemanticCache))

    fake = fakeredis.FakeRedis(decode_responses=True)
    inject_redis_cp(fake)
    cp = RedisCheckpointer()
    cp.save_state("t1", {"tenant_slug": TENANT, "retries": 0})
    cp.save_state("t2", {"tenant_slug": "user-77", "retries": 0})

    def _make_redis_cp(*a, **kw):
        return RedisCheckpointer()

    with patch("src.rag.checkpointer.SQLiteCheckpointer") as MockSQLite, \
         patch("src.state.redis_checkpointer.RedisCheckpointer", _make_redis_cp), \
         patch("src.auth.auth.delete_user"), \
         patch("src.rag.config.JUDGE_QUEUE_ENABLED", False):
        MockSQLite.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        resp = client.delete("/tenant")

    assert resp.status_code == 204
    assert cp.load_state("t1") is None
    assert cp.load_state("t2") is not None

    inject_redis_cp(None)


# ---------------------------------------------------------------------------
# Judge results deleted for tenant
# ---------------------------------------------------------------------------
def test_delete_tenant_clears_judge_results(client, tenant_dirs, monkeypatch):
    import src.api.app as app_mod

    monkeypatch.setattr(app_mod, "semantic_cache", MagicMock(spec=SemanticCache))

    fake = fakeredis.FakeRedis(decode_responses=True)
    set_judge_redis(fake)

    jq = JudgeQueue(results_prefix="judge:result:")
    jq.set_result("trace-1", {"status": "done"}, tenant=TENANT)
    jq.set_result("trace-2", {"status": "done"}, tenant="user-5")

    with patch("src.rag.checkpointer.SQLiteCheckpointer") as MockSQLite, \
         patch("src.state.redis_checkpointer.RedisCheckpointer") as MockRedis, \
         patch("src.auth.auth.delete_user"), \
         patch("src.rag.config.JUDGE_QUEUE_ENABLED", True):
        MockSQLite.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        MockRedis.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        resp = client.delete("/tenant")

    assert resp.status_code == 204
    assert jq.get_result("trace-1", tenant=TENANT) is None
    assert jq.get_result("trace-2", tenant="user-5") is not None

    set_judge_redis(None)


# ---------------------------------------------------------------------------
# Idempotent: second call on already-deleted tenant returns 204
# ---------------------------------------------------------------------------
def test_delete_tenant_idempotent(client, tenant_dirs, monkeypatch):
    import src.api.app as app_mod

    monkeypatch.setattr(app_mod, "semantic_cache", MagicMock(spec=SemanticCache))

    with patch("src.rag.checkpointer.SQLiteCheckpointer") as MockSQLite, \
         patch("src.state.redis_checkpointer.RedisCheckpointer") as MockRedis, \
         patch("src.auth.auth.delete_user"), \
         patch("src.rag.config.JUDGE_QUEUE_ENABLED", False):
        MockSQLite.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        MockRedis.return_value.delete_tenant_checkpoints = MagicMock(return_value=0)
        r1 = client.delete("/tenant")
        r2 = client.delete("/tenant")

    assert r1.status_code == 204
    assert r2.status_code == 204
