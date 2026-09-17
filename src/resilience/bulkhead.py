"""Bulkhead: limit concurrent calls to a named resource pool.

A bulkhead isolates one subsystem's concurrency from another.  Here, the LLM
call pool (MAIN_POOL_SIZE) and the judge/eval call pool (JUDGE_POOL_SIZE) are
separated so a spike in judge work cannot starve the primary answer path.

Each Bulkhead is a counting semaphore.  When the semaphore is exhausted,
acquire() raises BulkheadFullError immediately (non-blocking) so the caller
can return 503 rather than queuing indefinitely.

Thread-safe via threading.Semaphore.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class BulkheadFullError(Exception):
    """Raised when the pool is at capacity."""

    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = capacity
        super().__init__(f"bulkhead {name!r} at capacity ({capacity} concurrent calls)")


class Bulkhead:
    """Named concurrency bulkhead.

    Args:
        name:     used for logging.
        capacity: maximum concurrent callers.
    """

    def __init__(self, name: str, capacity: int) -> None:
        self._name = name
        self._capacity = capacity
        self._sem = threading.Semaphore(capacity)
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        return self._active

    @property
    def capacity(self) -> int:
        return self._capacity

    def acquire(self) -> None:
        """Acquire a slot; raises BulkheadFullError if none available."""
        acquired = self._sem.acquire(blocking=False)
        if not acquired:
            raise BulkheadFullError(self._name, self._capacity)
        with self._lock:
            self._active += 1
        logger.debug("bulkhead %r acquired (%d/%d)", self._name, self._active, self._capacity)

    def release(self) -> None:
        """Release a previously acquired slot."""
        with self._lock:
            self._active = max(0, self._active - 1)
        self._sem.release()
        logger.debug("bulkhead %r released (%d/%d)", self._name, self._active, self._capacity)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()

    def call(self, fn, *args, **kwargs):
        """Execute fn(*args, **kwargs) within the bulkhead; return its result."""
        with self:
            return fn(*args, **kwargs)


# Module-level singletons matching the config pool sizes.
def _make_main_bulkhead() -> Bulkhead:
    from src.rag.config import MAIN_POOL_SIZE
    return Bulkhead("main-llm", MAIN_POOL_SIZE)


def _make_judge_bulkhead() -> Bulkhead:
    from src.rag.config import JUDGE_POOL_SIZE
    return Bulkhead("judge", JUDGE_POOL_SIZE)


main_bulkhead: Bulkhead = _make_main_bulkhead()
judge_bulkhead: Bulkhead = _make_judge_bulkhead()
