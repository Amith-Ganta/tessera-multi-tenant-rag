"""Tests for the process-wide vectorstore cache in retriever_dense.

Part 1 of Phase 3: get_vectorstore() must return a cached singleton constructed
once per process (per tenant index dir), never once per call. These tests never
touch the network: OpenAIEmbeddings and Chroma are patched at the point of use
(src.rag.retriever_dense) with lightweight fakes, and get_openai_api_key is
stubbed so no real key is required.
"""

from __future__ import annotations

import threading

import pytest

import src.rag.retriever_dense as rd


class _FakeEmbeddings:
    """Stand-in for OpenAIEmbeddings; records nothing, costs nothing."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeChroma:
    """Stand-in for Chroma; a plain object so identity checks are meaningful."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """Reset the cache before and after each test, and stub the API key check."""
    monkeypatch.setattr(rd, "get_openai_api_key", lambda: "test-key")
    rd.reset_vectorstore_cache()
    yield
    rd.reset_vectorstore_cache()


def test_two_calls_return_same_object(monkeypatch):
    monkeypatch.setattr(rd, "OpenAIEmbeddings", _FakeEmbeddings)
    monkeypatch.setattr(rd, "Chroma", _FakeChroma)

    first = rd.get_vectorstore()
    second = rd.get_vectorstore()

    assert first is second


def test_reset_forces_reconstruction(monkeypatch):
    monkeypatch.setattr(rd, "OpenAIEmbeddings", _FakeEmbeddings)
    monkeypatch.setattr(rd, "Chroma", _FakeChroma)

    first = rd.get_vectorstore()
    rd.reset_vectorstore_cache()
    second = rd.get_vectorstore()

    assert first is not second


def test_concurrent_calls_construct_once(monkeypatch):
    construction_count = {"n": 0}
    count_lock = threading.Lock()

    class _CountingChroma(_FakeChroma):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with count_lock:
                construction_count["n"] += 1

    monkeypatch.setattr(rd, "OpenAIEmbeddings", _FakeEmbeddings)
    monkeypatch.setattr(rd, "Chroma", _CountingChroma)

    results: list[object] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()  # maximise contention: all threads hit get_vectorstore together
        store = rd.get_vectorstore()
        with results_lock:
            results.append(store)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert construction_count["n"] == 1
    assert len(results) == 5
    assert all(store is results[0] for store in results)


def test_embeddings_not_reinstantiated(monkeypatch):
    calls = {"n": 0}

    class _CountingEmbeddings(_FakeEmbeddings):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            calls["n"] += 1

    monkeypatch.setattr(rd, "OpenAIEmbeddings", _CountingEmbeddings)
    monkeypatch.setattr(rd, "Chroma", _FakeChroma)

    rd.get_vectorstore()
    rd.get_vectorstore()

    assert calls["n"] == 1


def test_chroma_not_reinstantiated(monkeypatch):
    calls = {"n": 0}

    class _CountingChroma(_FakeChroma):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            calls["n"] += 1

    monkeypatch.setattr(rd, "OpenAIEmbeddings", _FakeEmbeddings)
    monkeypatch.setattr(rd, "Chroma", _CountingChroma)

    rd.get_vectorstore()
    rd.get_vectorstore()

    assert calls["n"] == 1
