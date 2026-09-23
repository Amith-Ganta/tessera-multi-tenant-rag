"""Cost observability for Tessera.

Single source of truth for per-model token pricing and session spend tracking.
The in-process spend accumulator is intentionally not shared across replicas —
each replica tracks its own window. For cross-replica budgeting use Langfuse
trace-level cost fields or the analytics JSONL (both carry estimated_cost_usd).

Public API
----------
- RATES           : dict[str, float]  — per-model USD/1M-token rates
- estimate_usd()  : compute cost from model + token counts
- record()        : add to the session accumulator
- spend_so_far()  : read the accumulator
- reset()         : zero the accumulator (test helper / startup hook)
- daily_cap()     : configured hard cap in USD (0 = disabled)
- over_cap()      : True when spend_so_far() > daily_cap() > 0
"""
from __future__ import annotations

import os
from threading import Lock

# ---------------------------------------------------------------------------
# Per-model blended rates (USD per 1 000 000 tokens, prompt + completion).
# Update here and in docs/COST_MODEL.md when provider pricing changes.
# deepseek-flash rate uses ~5:1 input:output ratio typical for RAG workloads.
# ---------------------------------------------------------------------------
RATES: dict[str, float] = {
    "deepseek/deepseek-chat": 0.27,
    "deepseek/deepseek-flash": 0.25,
    "openai/gpt-4o-mini": 0.15,
    "openai/gpt-4o": 2.50,
}

_lock = Lock()
_session_usd: float = 0.0


def estimate_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated USD cost for one LLM call."""
    rate = RATES.get(model, 0.0)
    return ((prompt_tokens + completion_tokens) / 1_000_000) * rate


def record(usd: float) -> float:
    """Add usd to the session accumulator; return new total."""
    global _session_usd
    with _lock:
        _session_usd += max(0.0, usd)
        return _session_usd


def spend_so_far() -> float:
    """Return accumulated USD spend for this process since last reset."""
    with _lock:
        return _session_usd


def reset() -> None:
    """Zero the accumulator. Used in tests and server startup."""
    global _session_usd
    with _lock:
        _session_usd = 0.0


def daily_cap() -> float:
    """Hard daily spend cap in USD. 0 means disabled. Reads TESSERA_DAILY_SPEND_USD_CAP."""
    raw = os.getenv("TESSERA_DAILY_SPEND_USD_CAP", "5.0")
    try:
        return float(raw.strip())
    except ValueError:
        return 5.0


def over_cap() -> bool:
    """True when spend_so_far() exceeds daily_cap() and the cap is enabled."""
    cap = daily_cap()
    return cap > 0 and spend_so_far() > cap
