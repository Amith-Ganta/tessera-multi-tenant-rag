from __future__ import annotations

# deepeval is a dev/test dependency — it is NOT installed in the production image
# (uv sync --no-dev). Wrap the import so the module loads cleanly in production;
# evaluate_answer() degrades to {"enabled": False} when deepeval is absent.
try:
    from deepeval.metrics import (
        FaithfulnessMetric,
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        ContextualRelevancyMetric,
        ToxicityMetric,
        GEval,
    )
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams
    from deepeval.models import OpenAIModel
    _DEEPEVAL_AVAILABLE = True
except ModuleNotFoundError:
    _DEEPEVAL_AVAILABLE = False
    FaithfulnessMetric = AnswerRelevancyMetric = ContextualPrecisionMetric = None  # type: ignore[assignment]
    ContextualRecallMetric = ContextualRelevancyMetric = ToxicityMetric = GEval = None  # type: ignore[assignment]
    LLMTestCase = LLMTestCaseParams = OpenAIModel = None  # type: ignore[assignment]

from .config import get_openai_api_key

# Phase 3 (3b): time the DeepEval judge as its own stage. Guarded the same way the
# retrievers guard it: if the observability package is unavailable, time_stage
# degrades to a no-op so evaluation never breaks on an import error.
try:
    from observability import Stage, time_stage
except Exception:  # pragma: no cover - defensive import
    import contextlib as _contextlib

    class Stage:  # minimal shim; attribute access returns the stage name string
        JUDGE = "judge"

    @_contextlib.contextmanager
    def time_stage(*_args, **_kwargs):
        yield

_JUDGE = None
_JUDGE_ATTEMPTED = False

_FAITHFULNESS_THRESHOLD = 0.7
_ANSWER_RELEVANCY_THRESHOLD = 0.7
_CONTEXTUAL_PRECISION_THRESHOLD = 0.7
_CONTEXTUAL_RECALL_THRESHOLD = 0.7
_CONTEXTUAL_RELEVANCY_THRESHOLD = 0.7
_TOXICITY_THRESHOLD = 0.5
_CORRECTNESS_THRESHOLD = 0.7


def _judge():
    global _JUDGE, _JUDGE_ATTEMPTED

    if not _DEEPEVAL_AVAILABLE:
        return None
    if not _JUDGE_ATTEMPTED:
        _JUDGE_ATTEMPTED = True
        try:
            _JUDGE = OpenAIModel(model="gpt-4o-mini", api_key=get_openai_api_key())
        except Exception:
            _JUDGE = None

    return _JUDGE


def _run_metric(metrics: dict[str, dict], name: str, builder, case: LLMTestCase, threshold: float) -> None:
    try:
        metric = builder()
        # The measure() call is the actual LLM-as-judge round-trip; build() only
        # constructs the metric object. Time only the judge call so the JUDGE row
        # reflects real evaluation cost, not object construction.
        with time_stage(Stage.JUDGE):
            metric.measure(case)
        metrics[name] = {
            "score": float(metric.score),
            "passed": bool(metric.is_successful()),
            "reason": getattr(metric, "reason", None),
            "threshold": threshold,
        }
    except Exception as exc:
        metrics[name] = {"error": str(exc)}


def evaluate_answer(question: str, answer: str, contexts: list[str], expected_output: str | None = None) -> dict:
    if not _DEEPEVAL_AVAILABLE:
        return {"enabled": False, "reason": "deepeval not installed", "metrics": {}}
    judge = _judge()
    if judge is None:
        return {"enabled": False, "reason": "judge unavailable", "metrics": {}}

    case = LLMTestCase(
        input=question,
        actual_output=answer,
        retrieval_context=contexts or [],
        expected_output=expected_output,
    )

    metrics: dict[str, dict] = {}

    if contexts:
        _run_metric(
            metrics,
            "faithfulness",
            lambda: FaithfulnessMetric(
                threshold=_FAITHFULNESS_THRESHOLD,
                model=_judge(),
                include_reason=True,
            ),
            case,
            _FAITHFULNESS_THRESHOLD,
        )
    else:
        metrics["faithfulness"] = {"status": "skipped", "reason": "no contexts"}

    _run_metric(
        metrics,
        "answer_relevancy",
        lambda: AnswerRelevancyMetric(
            threshold=_ANSWER_RELEVANCY_THRESHOLD,
            model=_judge(),
            include_reason=True,
        ),
        case,
        _ANSWER_RELEVANCY_THRESHOLD,
    )

    if contexts:
        _run_metric(
            metrics,
            "contextual_relevancy",
            lambda: ContextualRelevancyMetric(
                threshold=_CONTEXTUAL_RELEVANCY_THRESHOLD,
                model=_judge(),
                include_reason=True,
            ),
            case,
            _CONTEXTUAL_RELEVANCY_THRESHOLD,
        )
    else:
        metrics["contextual_relevancy"] = {"status": "skipped", "reason": "no contexts"}

    if expected_output and contexts:
        _run_metric(
            metrics,
            "contextual_precision",
            lambda: ContextualPrecisionMetric(
                threshold=_CONTEXTUAL_PRECISION_THRESHOLD,
                model=_judge(),
                include_reason=True,
            ),
            case,
            _CONTEXTUAL_PRECISION_THRESHOLD,
        )
        _run_metric(
            metrics,
            "contextual_recall",
            lambda: ContextualRecallMetric(
                threshold=_CONTEXTUAL_RECALL_THRESHOLD,
                model=_judge(),
                include_reason=True,
            ),
            case,
            _CONTEXTUAL_RECALL_THRESHOLD,
        )
    else:
        metrics["contextual_precision"] = {"status": "skipped", "reason": "no expected_output"}
        metrics["contextual_recall"] = {"status": "skipped", "reason": "no expected_output"}

    _run_metric(
        metrics,
        "toxicity",
        lambda: ToxicityMetric(
            threshold=_TOXICITY_THRESHOLD,
            model=_judge(),
            include_reason=True,
        ),
        case,
        _TOXICITY_THRESHOLD,
    )

    _run_metric(
        metrics,
        "correctness",
        lambda: GEval(
            name="Correctness",
            criteria="Judge whether the answer correctly and completely addresses the question using only the given context, without contradicting it.",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            threshold=_CORRECTNESS_THRESHOLD,
            model=_judge(),
        ),
        case,
        _CORRECTNESS_THRESHOLD,
    )

    return {"enabled": True, "metrics": metrics}