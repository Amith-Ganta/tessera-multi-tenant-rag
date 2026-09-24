"""Phase 2 agentic retrieval pipeline with routing, fusion, rerank, and self-check."""

from __future__ import annotations

import json
import sys
from typing import Any

from langchain_core.documents import Document

from .config import CHAT_MODEL, RETRIEVER_TOP_K
from .llm import complete as llm_complete
from .reranker import rerank
from .retriever_hybrid import retrieve_hybrid
from .router import route_query, tavily_search


def _safe_parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}


def _document_sources(docs: list[Document]) -> list[str]:
    sources: list[str] = []
    for doc in docs:
        source = str(doc.metadata.get("source", ""))
        if source and source not in sources:
            sources.append(source)
    return sources


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt": 0, "completion": 0, "total": 0}
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)
    else:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    return {"prompt": prompt_tokens, "completion": completion_tokens, "total": total_tokens}


def _generate(question: str, contexts: list[Document], feedback: str | None = None) -> tuple[str, dict[str, int]]:
    context_text = "\n\n".join(doc.page_content for doc in contexts)
    system_content = "Answer only from the provided context. If the context is insufficient, say you do not know."
    if feedback:
        system_content += f" Use this feedback to improve the draft: {feedback}"
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"Question: {question}\n\nContext:\n{context_text}"},
    ]
    return llm_complete(CHAT_MODEL, messages)


def _self_check(question: str, answer: str, contexts: list[Document]) -> tuple[dict[str, Any], dict[str, int]]:
    context_text = "\n\n".join(doc.page_content for doc in contexts)
    messages = [
        {
            "role": "system",
            "content": (
                'Judge whether the answer is grounded in context and answers the question. '
                'Return strict JSON with keys grounded, answers_question, and feedback.'
            ),
        },
        {"role": "user", "content": f"Question: {question}\n\nAnswer: {answer}\n\nContext: {context_text}"},
    ]
    content, usage = llm_complete(CHAT_MODEL, messages, json_mode=True)
    return _safe_parse_json(content or "{}"), usage


def _run_self_check(question: str, answer: str, contexts: list[Document]) -> tuple[bool, bool, str, dict[str, int]]:
    usage = {"prompt": 0, "completion": 0, "total": 0}
    for _ in range(2):
        parsed, check_usage = _self_check(question, answer, contexts)
        usage["prompt"] += check_usage["prompt"]
        usage["completion"] += check_usage["completion"]
        usage["total"] += check_usage["total"]
        grounded = bool(parsed.get("grounded", False))
        answers_question = bool(parsed.get("answers_question", False))
        feedback = str(parsed.get("feedback", "")).strip()
        if feedback or grounded or answers_question:
            return grounded, answers_question, feedback, usage
    return False, False, "fallback after JSON parse failure", usage


def invoke(question: str, top_k: int = RETRIEVER_TOP_K, force_route: str | None = None) -> dict[str, Any]:
    trace: list[str] = []
    usage = {"prompt": 0, "completion": 0, "total": 0}
    if force_route and force_route in {"vector", "web", "direct"}:
        decision = type('RoutingDecision', (), {'route': force_route, 'reason': 'user-selected'})()
        trace.append(f"route={force_route}: user-selected")
    else:
        decision = route_query(question)
        route = decision.route
    route = decision.route
    trace.append(f"route={route}: {decision.reason}")

    if route == "web":
        docs, note = tavily_search(question, top_k=top_k)
        if not docs:
            trace.append(note)
            route = "vector"
            docs = retrieve_hybrid(question, top_k=top_k)
        else:
            trace.append(note)
    elif route == "direct":
        docs = []
    else:
        docs = retrieve_hybrid(question, top_k=top_k)

    reranked_docs = rerank(question, docs, top_k=top_k) if docs else []
    if reranked_docs != docs:
        trace.append("reranked contexts with cross-encoder")
    else:
        trace.append("retrieval completed")

    retries_used = 0
    answer, generate_usage = _generate(question, reranked_docs)
    usage["prompt"] += generate_usage["prompt"]
    usage["completion"] += generate_usage["completion"]
    usage["total"] += generate_usage["total"]
    grounded, answers_question, feedback, check_usage = _run_self_check(question, answer, reranked_docs)
    usage["prompt"] += check_usage["prompt"]
    usage["completion"] += check_usage["completion"]
    usage["total"] += check_usage["total"]
    trace.append(f"self-check grounded={grounded} answers_question={answers_question}")

    while retries_used < 2 and not (grounded and answers_question):
        retries_used += 1
        trace.append(f"retry={retries_used}: {feedback or 'self-check failed'}")
        answer, generate_usage = _generate(question, reranked_docs, feedback=feedback or "Improve grounding and answer completeness.")
        usage["prompt"] += generate_usage["prompt"]
        usage["completion"] += generate_usage["completion"]
        usage["total"] += generate_usage["total"]
        grounded, answers_question, feedback, check_usage = _run_self_check(question, answer, reranked_docs)
        usage["prompt"] += check_usage["prompt"]
        usage["completion"] += check_usage["completion"]
        usage["total"] += check_usage["total"]
        trace.append(f"self-check grounded={grounded} answers_question={answers_question}")

    return {
        "answer": answer,
        "route": route,
        "contexts": [doc.page_content for doc in reranked_docs],
        "sources": _document_sources(reranked_docs),
        "retries_used": retries_used,
        "trace": trace,
        "usage": usage,
    }


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit('Usage: uv run python -m src.rag.agent_pipeline "question"')
    result = invoke(sys.argv[1])
    print(result["answer"])
    print("\nTrace:")
    for line in result["trace"]:
        print(line)


if __name__ == "__main__":
    main()
