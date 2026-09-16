"""Thread-safe ring-buffer store for async judge results.

Keyed by trace_id (string).  Oldest entry is evicted once the store reaches
JUDGE_STORE_MAX entries.  All public methods are safe to call from any thread
or from within asyncio coroutines (the lock is a plain threading.Lock, which
is safe from async context because judge tasks run in the event-loop thread
and only store results; reads can come from any thread via FastAPI).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from src.rag.config import JUDGE_STORE_MAX

_PENDING = {"status": "pending"}


class JudgeStore:
    def __init__(self, max_size: int = JUDGE_STORE_MAX) -> None:
        self._max = max_size
        self._store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def set_pending(self, trace_id: str) -> None:
        with self._lock:
            if trace_id in self._store:
                self._store.move_to_end(trace_id)
            else:
                self._store[trace_id] = _PENDING
                self._evict()

    def set_result(self, trace_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._store[trace_id] = result
            self._store.move_to_end(trace_id)

    def get(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._store.get(trace_id)

    def _evict(self) -> None:
        while len(self._store) > self._max:
            self._store.popitem(last=False)


# Process-wide singleton used by the API and async runner.
judge_store = JudgeStore()
