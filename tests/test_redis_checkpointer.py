"""Tests for Phase 5 Part 4: Redis checkpointer (fakeredis-based)."""

from __future__ import annotations

import fakeredis
import pytest

from src.state.redis_checkpointer import RedisCheckpointer, _inject_client


@pytest.fixture(autouse=True)
def fake_redis():
    client = fakeredis.FakeRedis(decode_responses=True)
    _inject_client(client)
    yield client
    _inject_client(None)


def make_cp(**kwargs) -> RedisCheckpointer:
    return RedisCheckpointer(
        ttl=kwargs.get("ttl", 3600),
        key_prefix=kwargs.get("key_prefix", "test:ckpt:"),
    )


class TestRedisCheckpointer:
    def test_save_and_load(self, fake_redis):
        cp = make_cp()
        state = {"transcript": [{"role": "drafter", "content": "draft 1"}], "retries": 0}
        cp.save_state("tid-1", state)
        loaded = cp.load_state("tid-1")
        assert loaded is not None
        assert loaded["retries"] == 0
        assert len(loaded["transcript"]) == 1

    def test_load_missing_returns_none(self, fake_redis):
        cp = make_cp()
        assert cp.load_state("nonexistent") is None

    def test_overwrite_saves_latest(self, fake_redis):
        cp = make_cp()
        cp.save_state("tid-2", {"retries": 0, "last_draft": "v1"})
        cp.save_state("tid-2", {"retries": 1, "last_draft": "v2"})
        loaded = cp.load_state("tid-2")
        assert loaded is not None
        assert loaded["retries"] == 1
        assert loaded["last_draft"] == "v2"

    def test_delete_removes_state(self, fake_redis):
        cp = make_cp()
        cp.save_state("tid-3", {"retries": 0})
        deleted = cp.delete_state("tid-3")
        assert deleted is True
        assert cp.load_state("tid-3") is None

    def test_delete_missing_returns_false(self, fake_redis):
        cp = make_cp()
        assert cp.delete_state("nonexistent") is False
