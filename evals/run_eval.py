"""Run DeepEval on the retriever goldens and write a reproducible report.

The judge is gpt-4o-mini (a different model family from the deepseek generator),
so the eval does not self-judge and inflate its own scores. When a golden yields
retrieval context, we also measure Faithfulness (grounding); when a golden has both
context and an expected answer, we add ContextualPrecision and ContextualRecall.
Every metric runs inside its own guard and skips gracefully when its inputs are
missing, so a context-free golden never crashes the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

from deepeval.metrics import (
    AnswerRelevancyMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    FaithfulnessMetric,
    GEval,
)
from deepeval.models import OpenAIModel
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from src.rag.agent_pipeline import invoke
from src.rag.config import GOLDENS_PATH, PROJECT_ROOT, get_openai_api_key

REPORTS_DIR = PROJECT_ROOT / "evals" / "reports"
LATEST_REPORT_PATH = REPORTS_DIR / "latest.json"
RUN_LABEL = "phase3-eval"

# Reported model name. OpenAIModel may not expose get_model_name() in every
# version, so we use the literal we constructed it with rather than call a method
# we are not certain exists.
JUDGE_MODEL_NAME = "gpt-4o-mini"


def _load_goldens() -> list[dict[str, Any]]:
    with GOLDENS_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("goldens file must contain a list")
    return [item for item in data if isinstance(item, dict)]


def _safe_mean(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _short_reason(reason: str) -> str:
    text = reason.strip()
    return text if len(text) <= 220 else f"{text[:217]}..."


def _measure(metric, test_case, scores: list[float]) -> dict[str, Any]:
    """Run one metric under guard. Append the score on success; never raise."""
    row: dict[str, Any] = {"score": None, "passed": None, "reason": "", "error": "", "skipped": False}
    try:
        metric.measure(test_case)
        row["score"] = float(metric.score or 0.0)
        row["passed"] = bool(metric.is_successful())
        row["reason"] = _short_reason(metric.reason or "")
        scores.append(row["score"])
    except Exception as exc:  # pragma: no cover - defensive harness behavior
        row["error"] = str(exc)
    return row


def _skipped(note: str) -> dict[str, Any]:
    return {"score": None, "passed": None, "reason": note, "error": "", "skipped": True}


def _pass_rate(case_rows: list[dict[str, Any]], key: str) -> float:
    ran = [row for row in case_rows if row[key].get("passed") is not None]
    if not ran:
        return 0.0
    return sum(1 for row in ran if row[key]["passed"] is True) / len(ran)


def main() -> int:
    goldens = _load_goldens()
    judge = OpenAIModel(model="gpt-4o-mini", api_key=get_openai_api_key())

    case_rows: list[dict[str, Any]] = []
    relevancy_scores: list[float] = []
    correctness_scores: list[float] = []
    faithfulness_scores: list[float] = []
    context_precision_scores: list[float] = []
    context_recall_scores: list[float] = []

    for golden in goldens:
        case_id = str(golden.get("id", ""))
        question = str(golden.get("input", ""))
        expected_output = str(golden.get("expected_output", ""))

        result = invoke(question)
        answer = str(result.get("answer", ""))
        contexts_raw = result.get("contexts", [])
        contexts = [str(item) for item in contexts_raw] if isinstance(contexts_raw, list) else []
        has_context = len(contexts) > 0
        has_expected = bool(expected_output.strip())

        test_case = LLMTestCase(
            input=question,
            actual_output=answer,
            expected_output=expected_output,
            retrieval_context=contexts,
        )

        # Answered the question? (always applicable)
        relevancy = _measure(
            AnswerRelevancyMetric(threshold=0.7, model=judge, include_reason=True, async_mode=False),
            test_case,
            relevancy_scores,
        )

        # Correct vs the gold answer? (always applicable)
        correctness = _measure(
            GEval(
                name="Correctness",
                criteria=(
                    "Determine whether the actual output is factually consistent with and "
                    "covers the key facts in the expected output, given the question."
                ),
                evaluation_params=[
                    LLMTestCaseParams.INPUT,
                    LLMTestCaseParams.ACTUAL_OUTPUT,
                    LLMTestCaseParams.EXPECTED_OUTPUT,
                ],
                threshold=0.5,
                model=judge,
            ),
            test_case,
            correctness_scores,
        )

        # Grounded in the retrieved context? (needs context)
        if has_context:
            faithfulness = _measure(
                FaithfulnessMetric(threshold=0.7, model=judge, include_reason=True, async_mode=False),
                test_case,
                faithfulness_scores,
            )
        else:
            faithfulness = _skipped("no retrieval_context")

        # Retriever precision/recall (needs context AND expected answer)
        if has_context and has_expected:
            context_precision = _measure(
                ContextualPrecisionMetric(threshold=0.7, model=judge, include_reason=True, async_mode=False),
                test_case,
                context_precision_scores,
            )
            context_recall = _measure(
                ContextualRecallMetric(threshold=0.7, model=judge, include_reason=True, async_mode=False),
                test_case,
                context_recall_scores,
            )
        else:
            note = "no retrieval_context" if not has_context else "no expected_output"
            context_precision = _skipped(note)
            context_recall = _skipped(note)

        case_rows.append(
            {
                "id": case_id,
                "source": str(golden.get("source", "")),
                "route": str(result.get("route", "")),
                "retries_used": int(result.get("retries_used", 0) or 0),
                "relevancy": relevancy,
                "correctness": correctness,
                "faithfulness": faithfulness,
                "context_precision": context_precision,
                "context_recall": context_recall,
            }
        )

    # Correctness over vector-route items only: direct-route bypasses retrieval entirely,
    # so its correctness measures open-ended LLM quality, not RAG quality.  The gate uses
    # mean_correctness_vector so RAG regressions are not masked by direct-route noise.
    vector_correctness_scores = [
        row["correctness"]["score"]
        for row in case_rows
        if row["route"] == "vector"
        and row["correctness"].get("score") is not None
    ]

    aggregate_rows = {
        "count": len(case_rows),
        "mean_relevancy": _safe_mean(relevancy_scores),
        "mean_correctness": _safe_mean(correctness_scores),
        "mean_correctness_vector": _safe_mean(vector_correctness_scores),
        "mean_faithfulness": _safe_mean(faithfulness_scores),
        "mean_context_precision": _safe_mean(context_precision_scores),
        "mean_context_recall": _safe_mean(context_recall_scores),
        "pass_rate_relevancy": _pass_rate(case_rows, "relevancy"),
        "pass_rate_correctness": _pass_rate(case_rows, "correctness"),
        "pass_rate_faithfulness": _pass_rate(case_rows, "faithfulness"),
        "pass_rate_context_precision": _pass_rate(case_rows, "context_precision"),
        "pass_rate_context_recall": _pass_rate(case_rows, "context_recall"),
    }

    def _cell(row: dict[str, Any], key: str) -> str:
        metric = row[key]
        if metric.get("skipped"):
            return "skip"
        score = metric.get("score")
        return f"{score:.3f}" if isinstance(score, float) else "ERR"

    print(f"{'id':<4} {'route':<7} {'relev':<7} {'correct':<8} {'faith':<7}")
    for row in case_rows:
        print(
            f"{row['id']:<4} {row['route']:<7} "
            f"{_cell(row, 'relevancy'):<7} {_cell(row, 'correctness'):<8} "
            f"{_cell(row, 'faithfulness'):<7}"
        )

    metric_meta = {"threshold": 0.7, "model": JUDGE_MODEL_NAME}
    report = {
        "run": RUN_LABEL,
        "judge_model": JUDGE_MODEL_NAME,
        "metrics": {
            "answer_relevancy": {"threshold": 0.7, "model": JUDGE_MODEL_NAME},
            "correctness": {"threshold": 0.5, "model": JUDGE_MODEL_NAME},
            "faithfulness": dict(metric_meta),
            "context_precision": dict(metric_meta),
            "context_recall": dict(metric_meta),
        },
        "aggregates": aggregate_rows,
        "cases": case_rows,
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with LATEST_REPORT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
