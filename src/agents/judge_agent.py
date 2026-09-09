"""A2A Judge agent: scores a draft with the cross-family judge.

Runs as a standalone A2A server exposing the ``judge_answer`` skill. The skill
reuses the guard's judge logic in ``src/rag/answer_guard.judge_draft`` to score
faithfulness, correctness, and relevancy, and returns a gate verdict:
``{"score": {...}, "passed": bool, "feedback": str | None}``.

Run:

    uv run python -m src.agents.judge_agent
    # or: uv run uvicorn src.agents.judge_agent:app --port 8002
"""
from __future__ import annotations

import os

from src.rag.answer_guard import judge_draft

from .a2a_protocol import make_a2a_app

DEFAULT_PORT = int(os.environ.get("TESSERA_JUDGE_PORT", "8002"))
DEFAULT_URL = os.environ.get("TESSERA_JUDGE_URL", f"http://localhost:{DEFAULT_PORT}")


def judge_answer(
    draft: str,
    context: list,
    tenant_slug: str,
    question: str | None = None,
    expected_output: str | None = None,
) -> dict:
    """Score a draft using the existing cross-family judge from ``answer_guard``.

    ``question`` is needed for the relevancy and correctness metrics, so the
    supervisor passes it alongside the draft even though it is not part of the
    minimal A2A skill signature.
    """
    contexts = [str(item) for item in (context or [])]
    verdict = judge_draft(
        question=question or "",
        draft=str(draft or ""),
        contexts=contexts,
        expected_output=expected_output,
    )
    return {
        "score": verdict["score"],
        "passed": verdict["passed"],
        "feedback": verdict["feedback"],
        "tenant": tenant_slug,
        "enabled": verdict.get("enabled", False),
    }


def _handler(params: dict) -> dict:
    draft = str(params.get("draft", "") or "")
    context = params.get("context") or []
    if not isinstance(context, list):
        context = [context]
    tenant_slug = str(params.get("tenant_slug", "default")).strip() or "default"
    question = params.get("question") or None
    expected_output = params.get("expected_output") or None
    return judge_answer(
        draft=draft,
        context=context,
        tenant_slug=tenant_slug,
        question=question,
        expected_output=expected_output,
    )


app = make_a2a_app(
    name="Tessera Judge Agent",
    description="Scores a draft for faithfulness, correctness, and relevancy using the cross-family judge.",
    url=DEFAULT_URL,
    skill_id="judge_answer",
    skill_name="judge_answer",
    skill_description=(
        "Score a draft and return a gate verdict. Accepts draft, context, "
        "tenant_slug, and question; returns score, passed, and feedback."
    ),
    handler=_handler,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
