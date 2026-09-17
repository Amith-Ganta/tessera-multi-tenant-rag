"""Three-state circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED).

State transitions:
  CLOSED   → OPEN       after failure_threshold consecutive failures
  OPEN     → HALF_OPEN  after recovery_seconds have elapsed
  HALF_OPEN → CLOSED    on the next success
  HALF_OPEN → OPEN      on the next failure

Thread-safe via threading.Lock.  Backed by Redis so state is shared across
API replicas; falls back to in-process state if Redis is unavailable.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum

logger = logging.getLogger(__name__)

_CLOSED = "CLOSED"
_OPEN = "OPEN"
_HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is OPEN."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"circuit breaker {name!r} is OPEN — call rejected")


class CircuitBreaker:
    """Named circuit breaker.

    Args:
        name:               unique name (used as Redis key prefix and for logging).
        failure_threshold:  consecutive failures before opening.
        recovery_seconds:   seconds in OPEN before attempting HALF_OPEN.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int | None = None,
        recovery_seconds: int | None = None,
    ) -> None:
        from src.rag.config import CIRCUIT_BREAKER_FAILURE_THRESHOLD, CIRCUIT_BREAKER_RECOVERY_SECONDS
        self._name = name
        self._threshold = failure_threshold if failure_threshold is not None else CIRCUIT_BREAKER_FAILURE_THRESHOLD
        self._recovery = recovery_seconds if recovery_seconds is not None else CIRCUIT_BREAKER_RECOVERY_SECONDS

        # In-process state (used when Redis is unavailable).
        self._lock = threading.Lock()
        self._state = _CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        return self._state

    def _maybe_transition_to_half_open(self) -> None:
        if self._state == _OPEN and self._opened_at is not None:
            if time.time() - self._opened_at >= self._recovery:
                self._state = _HALF_OPEN
                logger.info("circuit %r → HALF_OPEN", self._name)

    def before_call(self) -> None:
        """Raises CircuitOpenError if the breaker is OPEN."""
        with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == _OPEN:
                raise CircuitOpenError(self._name)

    def on_success(self) -> None:
        """Record a success; closes the breaker if in HALF_OPEN."""
        with self._lock:
            if self._state == _HALF_OPEN:
                self._state = _CLOSED
                self._failures = 0
                logger.info("circuit %r → CLOSED (recovered)", self._name)
            elif self._state == _CLOSED:
                self._failures = 0

    def on_failure(self) -> None:
        """Record a failure; may open the breaker."""
        with self._lock:
            if self._state == _HALF_OPEN:
                self._state = _OPEN
                self._opened_at = time.time()
                logger.warning("circuit %r → OPEN (HALF_OPEN probe failed)", self._name)
                return

            self._failures += 1
            if self._failures >= self._threshold:
                self._state = _OPEN
                self._opened_at = time.time()
                logger.warning(
                    "circuit %r → OPEN after %d failures", self._name, self._failures
                )

    def call(self, fn, *args, **kwargs):
        """Execute fn(*args, **kwargs) under the breaker; return its result.

        Raises CircuitOpenError when open.  Records success/failure automatically.
        Any exception from fn is re-raised after being recorded as a failure.
        """
        self.before_call()
        try:
            result = fn(*args, **kwargs)
            self.on_success()
            return result
        except CircuitOpenError:
            raise
        except Exception:
            self.on_failure()
            raise

    def reset(self) -> None:
        """Reset to CLOSED (used in tests)."""
        with self._lock:
            self._state = _CLOSED
            self._failures = 0
            self._opened_at = None
