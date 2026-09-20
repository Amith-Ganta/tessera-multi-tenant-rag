"""Central LLM entry point: LiteLLM with provider fallback, cost caps, and tracing.

Every generation in this project goes through ``complete`` so that three
concerns live in one place instead of being copy-pasted across strategies:

1. Provider fallback. The primary model is DeepSeek. If a DeepSeek call fails
   (rate limit, timeout, transient 5xx), LiteLLM retries the same request on
   OpenAI ``gpt-4o-mini`` using that key. This is real failover, not a manual
   try/except, so the caller never sees a hard outage while either provider
   is healthy.
2. Cost control. A hard per-call output-token ceiling and a process-wide daily
   spend estimate are enforced here, so no single request and no runaway loop
   can spend without bound.
3. Observability. When LangSmith is configured, each call is traced with its
   model, token usage, and estimated cost. When it is not, tracing is a no-op
   and nothing else changes.

A hard per-call request timeout is applied so a stalled provider call fails
fast into the existing fallback chain instead of hanging.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from threading import Lock

import litellm
from litellm import completion

from .config import MODEL_CANARY_PERCENT, MODEL_CANARY_VERSION
from .models import resolve_model, MODEL_REGISTRY
from .tenant_context import active_tenant_id
from src.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError  # noqa: F401
from src.resilience.bulkhead import main_bulkhead

_llm_breaker = CircuitBreaker(name="llm")


def _select_model(default_model: str) -> str:
    """Return canary model if the active tenant hashes below the configured canary
    percent. Falls back to the default for requests without a tenant context, so
    system calls are never canaried."""
    if MODEL_CANARY_PERCENT <= 0 or not MODEL_CANARY_VERSION:
        return default_model
    tid = active_tenant_id()
    if tid is None:
        return default_model
    h = int(hashlib.sha256(tid.encode()).hexdigest(), 16) % 100
    if h < MODEL_CANARY_PERCENT:
        return MODEL_CANARY_VERSION
    return default_model

# Blended per-1M-token estimates, applied to (prompt + completion).
# These are estimates for the spend guard and analytics, not billed
# figures. The deepseek-flash rate assumes ~5:1 input:output ratio,
# which matches a retrieval-heavy RAG workload.
_USD_PER_1M_TOKENS = {
    "deepseek/deepseek-chat": 0.27,
    "deepseek/deepseek-flash": 0.25,
    "openai/gpt-4o-mini": 0.15,
    "openai/gpt-4o": 2.50,
}

def _env_num(name, default, cast):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        return default


# Per-call output ceiling. Keeps any one answer from ballooning.
MAX_OUTPUT_TOKENS = _env_num("TESSERA_MAX_OUTPUT_TOKENS", 1024, int)

# Hard per-call request timeout, so a stalled provider call fails fast.
REQUEST_TIMEOUT_SECONDS = _env_num("TESSERA_REQUEST_TIMEOUT_SECONDS", 30, float)

# Process-wide soft budget for a single run of the app. Once the estimated spend
# crosses this line, further LLM calls are refused with a clear error instead of
# silently continuing to bill. Set to 0 to disable the guard.
DAILY_SPEND_USD_CAP = _env_num("TESSERA_DAILY_SPEND_USD_CAP", 5.0, float)

# Fallback order by litellm id. DeepSeek flash is primary; OpenAI mini is the standby.
_FALLBACK_CHAIN = ["deepseek/deepseek-flash", "openai/gpt-4o-mini"]


@dataclass
class _Spend:
    usd: float = 0.0


_spend = _Spend()
_spend_lock = Lock()


def _estimate_usd(litellm_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = _USD_PER_1M_TOKENS.get(litellm_id, 0.0)
    return ((prompt_tokens + completion_tokens) / 1_000_000) * rate


def record_spend(usd: float) -> float:
    with _spend_lock:
        _spend.usd += max(0.0, usd)
        return _spend.usd


def spend_so_far() -> float:
    with _spend_lock:
        return _spend.usd


def _check_budget() -> None:
    if DAILY_SPEND_USD_CAP <= 0:
        return
    if spend_so_far() >= DAILY_SPEND_USD_CAP:
        raise RuntimeError(
            "daily spend cap reached: estimated "
            f"${spend_so_far():.4f} >= cap ${DAILY_SPEND_USD_CAP:.2f}. "
            "Raise TESSERA_DAILY_SPEND_USD_CAP or wait for the next run."
        )


def _keys_for_fallback() -> dict[str, str]:
    """Collect the api keys the fallback chain may need, keyed by litellm id."""
    keys: dict[str, str] = {}
    for name, entry in MODEL_REGISTRY.items():
        litellm_id = entry["litellm_id"]
        if litellm_id in _FALLBACK_CHAIN and litellm_id not in keys:
            try:
                _resolved_id, api_key = resolve_model(name)
                keys[litellm_id] = api_key
            except Exception:
                # A missing standby key simply drops that link from the chain.
                continue
    return keys


def _usage_dict(response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt": 0, "completion": 0, "total": 0}
    if isinstance(usage, dict):
        p = int(usage.get("prompt_tokens", 0) or 0)
        c = int(usage.get("completion_tokens", 0) or 0)
        t = int(usage.get("total_tokens", 0) or 0)
    else:
        p = int(getattr(usage, "prompt_tokens", 0) or 0)
        c = int(getattr(usage, "completion_tokens", 0) or 0)
        t = int(getattr(usage, "total_tokens", 0) or 0)
    return {"prompt": p, "completion": c, "total": t}


def complete(
    model: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = False,
    temperature: float = 0,
) -> tuple[str, dict[str, int]]:
    """Run one chat completion with fallback, a token cap, and a spend guard.

    Returns (content, usage). ``usage`` has prompt/completion/total int counts.
    Raises RuntimeError if the process-wide spend cap is already reached.
    """
    _check_budget()

    model = _select_model(model)
    primary_id, primary_key = resolve_model(model)

    # Build the fallback list: every standby whose key is available, minus the
    # primary itself. Each fallback carries its own api key so LiteLLM can switch
    # providers cleanly.
    standby_keys = _keys_for_fallback()
    fallbacks = []
    for litellm_id in _FALLBACK_CHAIN:
        if litellm_id == primary_id:
            continue
        if litellm_id in standby_keys:
            fallbacks.append({"model": litellm_id, "api_key": standby_keys[litellm_id]})

    kwargs: dict = dict(
        model=primary_id,
        messages=messages,
        api_key=primary_key,
        temperature=temperature,
        max_tokens=MAX_OUTPUT_TOKENS,
        num_retries=2,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if fallbacks:
        kwargs["fallbacks"] = fallbacks
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    from .observability import trace_llm

    with trace_llm(primary_id, messages) as span:
        with main_bulkhead:
            response = _llm_breaker.call(completion, **kwargs)
        content = response.choices[0].message.content or ""
        usage = _usage_dict(response)
        served_by = getattr(response, "model", primary_id) or primary_id
        cost = _estimate_usd(served_by, usage["prompt"], usage["completion"])
        record_spend(cost)
        span.finish(usage=usage, model=served_by, estimated_cost_usd=cost)

    return content, usage


def complete_stream(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0,
    on_token: "Callable[[str], None] | None" = None,
) -> tuple[str, dict[str, int]]:
    """Run one chat completion with streaming, yielding tokens via on_token callback.

    Calls LiteLLM with stream=True; each text chunk is passed to on_token as it
    arrives. Returns (full_content, usage) after the stream is exhausted.
    Usage comes from the final streamed chunk (stream_options usage_delta).
    """
    from typing import Callable  # noqa: F401 — type hint only
    _check_budget()

    primary_id, primary_key = resolve_model(model)

    standby_keys = _keys_for_fallback()
    fallbacks = []
    for litellm_id in _FALLBACK_CHAIN:
        if litellm_id == primary_id:
            continue
        if litellm_id in standby_keys:
            fallbacks.append({"model": litellm_id, "api_key": standby_keys[litellm_id]})

    kwargs: dict = dict(
        model=primary_id,
        messages=messages,
        api_key=primary_key,
        temperature=temperature,
        max_tokens=MAX_OUTPUT_TOKENS,
        num_retries=2,
        timeout=REQUEST_TIMEOUT_SECONDS,
        stream=True,
        stream_options={"include_usage": True},
    )
    if fallbacks:
        kwargs["fallbacks"] = fallbacks

    chunks_text: list[str] = []
    usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

    response_stream = completion(**kwargs)
    served_by = primary_id
    for chunk in response_stream:
        delta_content = chunk.choices[0].delta.content if chunk.choices else None
        if delta_content:
            chunks_text.append(delta_content)
            if on_token is not None:
                on_token(delta_content)
        # Capture usage from final chunk (some providers send it on the last chunk)
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = _usage_dict(chunk)
        # Track actual model served
        if getattr(chunk, "model", None):
            served_by = chunk.model

    full_content = "".join(chunks_text)
    cost = _estimate_usd(served_by, usage["prompt"], usage["completion"])
    record_spend(cost)
    return full_content, usage
