"""A2A Drafter agent: drafts an answer from hybrid retrieval + generation.

Runs as a standalone A2A server exposing the ``draft_answer`` skill. The skill
combines any prior judge feedback (and the previous draft) with the question,
retrieves context through the hybrid retriever scoped to the tenant, and produces
a grounded draft through the shared generation choke point (which carries
DeepSeek -> OpenAI provider fallback).

Run:

    uv run python -m src.agents.drafter_agent
    # or: uv run uvicorn src.agents.drafter_agent:app --port 8001
"""
from __future__ import annotations

import os

from src.rag.models import DEFAULT_MODEL
from src.rag.config import RETRIEVER_TOP_K
from src.rag.strategies import run_strategy
from src.rag.tenant_context import use_tenant

from .a2a_protocol import make_a2a_app

DEFAULT_PORT = int(os.environ.get("TESSERA_DRAFTER_PORT", "8001"))
DEFAULT_URL = os.environ.get("TESSERA_DRAFTER_URL", f"http://localhost:{DEFAULT_PORT}")


def draft_answer(
    question: str,
    tenant_slug: str,
    feedback: str | None = None,
    previous_draft: str | None = None,
    *,
    top_k: int = RETRIEVER_TOP_K,
    model: str = DEFAULT_MODEL,
    force_route: str = "vector",
) -> dict:
    """Produce a grounded draft for ``question`` inside the tenant scope.

    ``run_strategy("adaptive", ...)`` is the existing Drafter/Generator pipeline:
    it fuses dense + BM25 retrieval, reranks, sanitises the context against prompt
    injection, and generates through ``complete`` (provider fallback included).
    Feedback and the previous draft are folded into the generator's prompt.
    """
    combined_feedback: str | None = None
    feedback_parts: list[str] = []
    if feedback:
        feedback_parts.append(feedback)
    if previous_draft:
        feedback_parts.append(f"Previous draft to improve:\n{previous_draft}")
    if feedback_parts:
        combined_feedback = "\n".join(feedback_parts)

    with use_tenant(tenant_slug):
        result = run_strategy(
            "adaptive",
            question,
            top_k=top_k,
            model=model,
            force_route=force_route,
            feedback=combined_feedback,
        )

    return {
        "draft": result.get("answer", ""),
        "context": list(result.get("contexts", []) or []),
        "tenant": tenant_slug,
        "route": result.get("route", ""),
        "sources": list(result.get("sources", []) or []),
        "usage": dict(result.get("usage", {}) or {}),
        "trace": list(result.get("trace", []) or []),
    }


def _handler(params: dict) -> dict:
    question = str(params.get("question", "")).strip()
    if not question:
        raise ValueError("question is required")

    tenant_slug = str(params.get("tenant_slug", "default")).strip() or "default"
    feedback = params.get("feedback") or None
    previous_draft = params.get("previous_draft") or None
    top_k = int(params.get("top_k", RETRIEVER_TOP_K) or RETRIEVER_TOP_K)
    model = str(params.get("model", DEFAULT_MODEL) or DEFAULT_MODEL)
    force_route = str(params.get("force_route", "vector") or "vector")

    return draft_answer(
        question=question,
        tenant_slug=tenant_slug,
        feedback=feedback,
        previous_draft=previous_draft,
        top_k=top_k,
        model=model,
        force_route=force_route,
    )


app = make_a2a_app(
    name="Tessera Drafter Agent",
    description="Drafts a grounded answer from the tenant corpus using hybrid retrieval and generation.",
    url=DEFAULT_URL,
    skill_id="draft_answer",
    skill_name="draft_answer",
    skill_description=(
        "Draft an answer from retrieved context. Accepts question, tenant_slug, "
        "feedback, and previous_draft; returns draft, context, and tenant."
    ),
    handler=_handler,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
