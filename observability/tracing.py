"""No-op-safe child-span emitters for LangSmith and Langfuse.

The Phase 1 spec asks for each stage to be logged as a child span. This project
already traces runs to LangSmith (see ``src/rag/observability.py``); the user
asked to emit to BOTH LangSmith and Langfuse. Neither is a hard dependency of the
request path: each emitter activates only when its keys are present and its
package imports, and every call is wrapped so a tracing failure can never raise
into the pipeline or distort a timing measurement.

A "child span" here is deliberately lightweight -- a short-lived run/observation
named ``stage:<name>`` carrying the stage and its duration. We do not try to nest
it under the request's root span object, because the timing context managers live
deep inside ``src/rag/strategies.py`` where that root span is not in scope;
correlating by request happens later (Phase 1 keeps the local ring buffer as the
source of truth for percentiles).
"""

from __future__ import annotations

import os
from typing import Any, Optional


# ---------------------------------------------------------------------------
# LangSmith
# ---------------------------------------------------------------------------
def _langsmith_enabled() -> bool:
    if os.getenv("LANGSMITH_TRACING", "").lower() in {"false", "0", "no"}:
        return False
    return bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))


def _langsmith_project() -> str:
    return os.getenv("LANGSMITH_PROJECT", "tessera-rag")


def emit_langsmith_stage(stage: str, duration_ms: float, extra: Optional[dict] = None) -> None:
    """Record one stage as a LangSmith run. Silent no-op when unconfigured."""
    if not _langsmith_enabled():
        return
    try:
        from langsmith import Client

        client = Client()
        run = client.create_run(
            name=f"stage:{stage}",
            run_type="tool",
            inputs={"stage": stage},
            project_name=_langsmith_project(),
        )
        run_id = getattr(run, "id", None) or run
        outputs: dict[str, Any] = {"duration_ms": round(float(duration_ms), 3)}
        if extra:
            outputs.update(extra)
        client.update_run(run_id, outputs=outputs)
    except Exception:
        # Tracing must never break the request or the measurement.
        return


# ---------------------------------------------------------------------------
# Langfuse
# ---------------------------------------------------------------------------
def _langfuse_enabled() -> bool:
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    )


_LANGFUSE_CLIENT = None
_LANGFUSE_TRIED = False


def _langfuse_client():
    """Lazily construct a single Langfuse client; cache the outcome.

    Returns None (cached) when the package is missing or construction fails, so
    repeated stage calls do not retry a broken import on every request.
    """
    global _LANGFUSE_CLIENT, _LANGFUSE_TRIED
    if _LANGFUSE_TRIED:
        return _LANGFUSE_CLIENT
    _LANGFUSE_TRIED = True
    try:
        from langfuse import Langfuse

        # Accept either LANGFUSE_HOST or LANGFUSE_BASE_URL. Different Langfuse
        # docs/SDK generations name this variable differently, and this project's
        # .env supplies LANGFUSE_BASE_URL. Prefer LANGFUSE_HOST when both are set,
        # then fall back to LANGFUSE_BASE_URL, then the public cloud default.
        host = (
            os.getenv("LANGFUSE_HOST")
            or os.getenv("LANGFUSE_BASE_URL")
            or "https://cloud.langfuse.com"
        )
        _LANGFUSE_CLIENT = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=host,
        )
    except Exception:
        _LANGFUSE_CLIENT = None
    return _LANGFUSE_CLIENT


def emit_langfuse_stage(stage: str, duration_ms: float, extra: Optional[dict] = None) -> None:
    """Record one stage as a standalone Langfuse span. Silent no-op when off."""
    if not _langfuse_enabled():
        return
    client = _langfuse_client()
    if client is None:
        return
    try:
        metadata = {"duration_ms": round(float(duration_ms), 3)}
        if extra:
            metadata.update(extra)
        # The langfuse SDK surface has shifted across major versions; probe the
        # available entry point rather than pinning to one signature.
        if hasattr(client, "span"):
            span = client.span(name=f"stage:{stage}", metadata=metadata)
            end = getattr(span, "end", None)
            if callable(end):
                end()
        elif hasattr(client, "trace"):
            trace = client.trace(name=f"stage:{stage}", metadata=metadata)
            child = getattr(trace, "span", None)
            if callable(child):
                child(name=f"stage:{stage}", metadata=metadata)
    except Exception:
        return


def emit_stage_spans(stage: str, duration_ms: float, extra: Optional[dict] = None) -> None:
    """Fan a completed stage measurement out to every configured backend."""
    emit_langsmith_stage(stage, duration_ms, extra)
    emit_langfuse_stage(stage, duration_ms, extra)
